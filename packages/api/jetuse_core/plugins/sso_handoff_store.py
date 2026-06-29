"""SSO ハンドオフコード（単回使用・短 TTL）の保管庫（external-app SSO / BE06-SSO-002）。

OIDC SSO の引き渡しは **認可コード型**にする: `sso-exchange` が実 token-exchange で得た **発行
id_token をブラウザに直接返さず**、単回使用・短 TTL の **handoff code** に束ねて保持する。ブラウザ
には code（不透明・推測不能）だけを渡し、連携先が **バックチャネル** `sso-redeem` で code を1回だけ
交換して id_token を受け取る。再使用・期限切れは fail-closed（None）。

**保管の差し替え境界（BE06-MAJ-002）**: 保管は `HandoffStore` プロトコルへ抽象化し、ルートは
モジュール変数を直参照せず `get_store()` で解決する。既定はプロセスローカル（in-memory）の
`InMemoryHandoffStore`。**本番のマルチインスタンス運用では共有ストア（Redis/DB 等の TTL 付き原子
ストア）を `set_store()` で注入する**（複数 worker/インスタンスで exchange と redeem が別プロセスに
配送されても 404 にならないようにする。配備は実運用設定＝人間ゲート）。mint/redeem は単回交換が
原子的であること（同時 redeem でも 1 回だけ成功）を契約とする。code は `secrets.token_urlsafe` で
生成し、推測不能・単回使用・短命であることを id_token 漏洩対策の主たる制御とする。
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

#: handoff code の既定 TTL（秒）。SSO 引き渡しは即時遷移が前提なので短くする。
DEFAULT_TTL_SECONDS = 120
#: code のエントロピー（バイト）。token_urlsafe(32) ≒ 43 文字の URL-safe 文字列。
_CODE_BYTES = 32
#: 保管容量の上限（DoS/メモリ枯渇の上限。超過時は最古を1件追い出す）。
MAX_ENTRIES = 10000


@dataclass
class _Entry:
    app: str
    id_token: str
    subject: str
    issued_token_type: str
    expires_at: float  # time.monotonic() ベースの満了時刻
    mapped_claims: dict  # claimMapping 適用済みクレーム（groups→roles 等）。code に束ねて渡す


def _now() -> float:
    """単調増加クロック（テストで monkeypatch して TTL/期限掃除を検証できるよう関数化）。"""
    return time.monotonic()


@runtime_checkable
class HandoffStore(Protocol):
    """handoff code ストアの差し替え境界（BE06-MAJ-002）。

    実装は **原子的な mint/単回 redeem** を保証する。本番は共有ストア（Redis/DB）を注入する。
    """

    def mint(
        self,
        *,
        app: str,
        id_token: str,
        subject: str,
        issued_token_type: str,
        mapped_claims: dict | None = ...,
        ttl_seconds: int = ...,
    ) -> str:
        """発行 id_token を単回使用 code に束ねて保持し code を返す。"""
        ...

    def redeem(self, code: str, *, app: str) -> _Entry | None:
        """code を **1回だけ**交換してエントリを返す。無効/期限切れ/別アプリは None。"""
        ...


class InMemoryHandoffStore:
    """プロセスローカル（in-memory）の既定実装。単一プロセス/単一 worker 向け。

    マルチインスタンスでは exchange と redeem が別プロセスに配送されると redeem が 404 になるため、
    本番は共有ストアを `set_store()` で差し替える（人間ゲート＝実運用設定）。mint/redeem はロックで
    保護し、redeem は取り出し時に即削除して **同時 redeem でも 1 回だけ成功**（原子的単回交換）。
    """

    def __init__(self) -> None:
        self._store: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    def _sweep_expired_locked(self) -> None:
        """期限切れエントリを全件掃除する（_lock 保持下で呼ぶ）。未 redeem の token を残さない。"""
        now = _now()
        for code in [c for c, e in self._store.items() if now > e.expires_at]:
            del self._store[code]

    def mint(
        self,
        *,
        app: str,
        id_token: str,
        subject: str,
        issued_token_type: str,
        mapped_claims: dict | None = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> str:
        if not app or not isinstance(id_token, str) or not id_token:
            raise ValueError("app と id_token は必須")
        code = secrets.token_urlsafe(_CODE_BYTES)
        with self._lock:
            # 期限切れを掃除（未 redeem の id_token を残さない。BE06-MAJ-001）。
            self._sweep_expired_locked()
            # 容量上限（メモリ枯渇の上限）。超過時は最古（挿入順先頭）を追い出す。
            while len(self._store) >= MAX_ENTRIES:
                self._store.pop(next(iter(self._store)))
            self._store[code] = _Entry(
                app=app,
                id_token=id_token,
                subject=subject,
                issued_token_type=issued_token_type,
                expires_at=_now() + max(1, ttl_seconds),
                mapped_claims=dict(mapped_claims or {}),
            )
        return code

    def redeem(self, code: str, *, app: str) -> _Entry | None:
        if not code or not app:
            return None
        with self._lock:
            entry = self._store.get(code)
            if entry is None:
                return None
            # 取り出した時点で削除（成功・失敗いずれでも再使用させない＝単回使用・原子的）。
            del self._store[code]
        if entry.app != app:
            return None  # 別アプリ向けの code を流用させない
        if _now() > entry.expires_at:
            return None  # 期限切れ
        return entry

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


# --- 差し替え境界（DI seam。本番は共有ストアを set_store で注入） ----------------

_DEFAULT_STORE = InMemoryHandoffStore()
_active_store: HandoffStore = _DEFAULT_STORE


def get_store() -> HandoffStore:
    """現在有効な handoff ストアを返す（ルートはこれ経由で mint/redeem する）。"""
    return _active_store


def set_store(store: HandoffStore | None) -> None:
    """handoff ストアを差し替える（本番＝共有ストア注入 / None で既定へ戻す）。"""
    global _active_store
    _active_store = store if store is not None else _DEFAULT_STORE


# --- 後方互換のモジュール関数（有効ストアへ委譲） ------------------------------


def mint(
    *,
    app: str,
    id_token: str,
    subject: str,
    issued_token_type: str,
    mapped_claims: dict | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> str:
    """発行 id_token を単回使用 code に束ねて保持し code を返す（有効ストアへ委譲）。

    `mapped_claims`（claimMapping 適用済み。groups→roles 等）も code に束ねて改ざん不能に保持し、
    redeem 応答で外部アプリへ渡す（実 SSO セッションに roles を反映できる。BE06-MAJ-003）。
    """
    return _active_store.mint(
        app=app,
        id_token=id_token,
        subject=subject,
        issued_token_type=issued_token_type,
        mapped_claims=mapped_claims,
        ttl_seconds=ttl_seconds,
    )


def redeem(code: str, *, app: str) -> _Entry | None:
    """code を **1回だけ**交換してエントリを返す（有効ストアへ委譲）。

    対象アプリ一致・未期限・未使用を検証し、いずれか欠ければ None（fail-closed）。
    """
    return _active_store.redeem(code, app=app)


def _clear_for_test() -> None:
    """テスト用に保管庫を空にし、既定ストアへ戻す（プロセスローカル状態の漏れ込みを防ぐ）。"""
    set_store(None)
    _DEFAULT_STORE.clear()
