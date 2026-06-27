"""中央レジストリのドメインサービス(list/search/get/download/publish ＋ MKT-02 拡張)。

PLG-04 は Object Storage + index.json を直接持っていたが、MKT-02 では永続化を
`RegistryBackend`(`backend.py`)へ委譲し、**ADB バックエンドμService へ昇格**できるようにした。
service は HTTP にもバックエンド実装にも依存せず、認証・manifest 検証・ed25519 署名検証・認可
(publisher 一致)・ライフサイクルに基づく可視性判断という「意味づけ」を担う。

後方互換: `RegistryService(store, authenticator)` は従来どおり Object Storage + index.json
(`IndexBackend`)で動く。新しい ADB μService は `RegistryService(authenticator=auth, backend=adb)`
で構築する。list/get/download/publish の外部契約は PLG-04 と同一。

publish の認可・検証フロー(無署名拒否を含む):
  1. 発行者トークンを authenticate(失敗→RegistryAuthError)。
  2. manifest を PLG-01 で検証(失敗→RegistryValidationError)。
  3. manifest.publisher が認証 publisher と一致(不一致→RegistryForbiddenError=403。なりすまし防止)。
  4. signature が存在(無ければ→RegistryValidationError = **無署名 publish 拒否**)。
  5. signature.publicKeyId に対応する登録公開鍵を引く(未登録→RegistryValidationError)。
  6. verify_signature が True(改ざん/不一致→RegistryValidationError)。
  7. backend.add_version で (id, version) を原子的に追加(既存→RegistryConflictError。版は不変)。

MKT-02 拡張(ADB バックエンド限定): 評価(rate/get_ratings)・DL 数(download で加算)・
版ライフサイクル(active/deprecated/yanked)・DB 検索(backend.search)。
"""

from __future__ import annotations

import base64
import binascii
import datetime
import hashlib
from typing import Any

from jetuse_core.plugins.manifest import (
    ManifestError,
    PluginManifest,
    validate_manifest,
    verify_signature,
)

from . import semver
from .backend import MAX_RATING, MIN_RATING, RatingSummary, RegistryBackend
from .errors import (
    RegistryAuthError,
    RegistryForbiddenError,
    RegistryGoneError,
    RegistryNotFoundError,
    RegistryValidationError,
)
from .index import (
    LIFECYCLE_ACTIVE,
    LIFECYCLE_DEPRECATED,
    LIFECYCLE_STATES,
    LIFECYCLE_YANKED,
    IndexEntry,
    PublisherKey,
)
from .index_backend import IndexBackend
from .storage import ObjectStore

#: ed25519 raw 公開鍵のバイト長。
_ED25519_PUBLIC_KEY_LEN = 32


def _utcnow_iso() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def _artifact_path(plugin_id: str, version: str, digest: str) -> str:
    # id は namespace/name。保存層オブジェクト名にスラッシュ階層として展開する。
    # 成果物パスに sha256 を含める(content-addressed)。publish が index 確定前に失敗して成果物だけ
    # 残っても(orphan)、同一 (id,version) を別内容で再 publish した際に衝突しない(版の汚染防止)。
    return f"plugins/{plugin_id}/{version}/{digest}.json"


def _decode_key_or_none(b64: str) -> bytes | None:
    """base64 公開鍵をデコードする。不正なら None(鍵の等価判定で「不一致」に倒す)。"""
    try:
        return base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        return None


def _manifest_bytes(manifest: PluginManifest) -> bytes:
    """成果物として保存する manifest の正準バイト列(配布表現 camelCase, 安定整形)。"""
    import json

    data = manifest.model_dump(by_alias=True)
    return json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")


class RegistryService:
    """`RegistryBackend` を保存層とする中央レジストリ(認証・署名検証・認可・可視性判断)。"""

    def __init__(
        self,
        store: ObjectStore | None = None,
        authenticator: Any = None,
        *,
        backend: RegistryBackend | None = None,
    ) -> None:
        """後方互換: `RegistryService(store, auth)` は IndexBackend(index.json)で動く。

        ADB μService は `RegistryService(authenticator=auth, backend=AdbBackend(...))` で構築する。
        store と backend の同時指定は曖昧なので拒否する(どちらが正本か一意にする)。
        """
        if authenticator is None:
            raise ValueError("authenticator は必須")
        if backend is not None:
            if store is not None:
                raise ValueError("store と backend は同時に指定できない(どちらか一方)")
            self._backend: RegistryBackend = backend
        else:
            if store is None:
                raise ValueError("store または backend のいずれかが必要")
            self._backend = IndexBackend(store)
        self._auth = authenticator

    # --- 読取 API(公開) ---

    def list_plugins(self, *, include_yanked: bool = False) -> list[dict[str, Any]]:
        """登録済みの全プラグイン版メタデータを返す(発行者鍵は含めない)。

        既定では yanked(配布取り下げ)を除外する。include_yanked=True で監査用に全件返す。
        """
        return [
            e.model_dump(by_alias=True)
            for e in self._backend.list_entries()
            if include_yanked or e.lifecycle != LIFECYCLE_YANKED
        ]

    def search(
        self,
        q: str | None = None,
        *,
        kind: str | None = None,
        tag: str | None = None,
        include_yanked: bool = False,
    ) -> list[dict[str, Any]]:
        """q(id/name/description 部分一致)・kind・tag で絞り込んだ一覧を返す。

        絞り込みはバックエンドに委譲する(ADB は SQL、index はインメモリ)。yanked は既定で除外する。
        """
        return [
            e.model_dump(by_alias=True)
            for e in self._backend.search(q, kind=kind, tag=tag)
            if include_yanked or e.lifecycle != LIFECYCLE_YANKED
        ]

    def get(self, plugin_id: str, version: str | None = None) -> dict[str, Any]:
        """エントリ＋manifest 全文を返す。version 省略時は最新(active 優先・yanked 除外)を選ぶ。"""
        entry = self._resolve_entry(plugin_id, version)
        manifest = self._load_manifest(entry)
        return {
            "entry": entry.model_dump(by_alias=True),
            "manifest": manifest,
        }

    def download(self, plugin_id: str, version: str | None = None) -> tuple[bytes, IndexEntry]:
        """成果物(manifest 全文)のバイト列とエントリを返す。version 省略時は最新。

        ADB バックエンドでは DL 数を原子的に加算し返すエントリへ反映する(index バックエンドは据置)。
        """
        entry = self._resolve_entry(plugin_id, version)
        data = self._backend.read_artifact(entry)
        new_count = self._backend.record_download(entry.id, entry.version)
        if new_count is not None:
            # 加算後カウントを返り値へ反映(レスポンスヘッダ用)。元エントリは不変なのでコピーを返す。
            entry = entry.model_copy(update={"download_count": new_count})
        return data, entry

    def _resolve_entry(self, plugin_id: str, version: str | None) -> IndexEntry:
        """取得対象エントリを解決する(ライフサイクル考慮)。

        - version 指定: 該当版を返す。yanked は 410(配布取り下げ)。
        - version 省略(最新): active 優先、無ければ deprecated にフォールバック。yanked は対象外。
          解決不能(全 yanked / 不在)は 404。
        """
        if version is not None:
            entry = self._backend.find(plugin_id, version)
            if entry is None:
                raise RegistryNotFoundError(f"{plugin_id}@{version} は存在しない")
            if entry.lifecycle == LIFECYCLE_YANKED:
                raise RegistryGoneError(
                    f"{plugin_id}@{version} は yank 済みで配布停止(版は不変だが新規取得は不可)"
                )
            return entry
        candidates = self._backend.versions(plugin_id)
        if not candidates:
            raise RegistryNotFoundError(f"{plugin_id} は存在しない")
        entry = self._pick_latest(candidates)
        if entry is None:
            # 版は在るが全て yanked。latest としては解決不能。
            raise RegistryNotFoundError(f"{plugin_id} に配布中の版が無い(全て yank 済み)")
        return entry

    @staticmethod
    def _pick_latest(entries: list[IndexEntry]) -> IndexEntry | None:
        """latest を選ぶ。active 優先・無ければ deprecated・yanked は除外。空なら None。"""
        active = [e for e in entries if e.lifecycle == LIFECYCLE_ACTIVE]
        pool = active or [e for e in entries if e.lifecycle == LIFECYCLE_DEPRECATED]
        if not pool:
            return None
        latest_version = semver.latest([e.version for e in pool])
        for e in pool:
            if e.version == latest_version:
                return e
        return None  # 到達しない(latest は pool の version から選ぶ)。

    def _load_manifest(self, entry: IndexEntry) -> dict[str, Any]:
        import json

        return json.loads(self._backend.read_artifact(entry).decode("utf-8"))

    def get_publisher_keys(self, publisher: str) -> list[dict[str, str]]:
        """発行者の登録済み公開鍵(publicKeyId + 公開鍵)を返す。取込側(PLG-03)の署名検証用。

        公開鍵は秘匿情報ではなく、取込クライアントが manifest.signature を検証するために必要
        (specs/16 §6 / ADR-0013)。read 系として無認証で公開する。該当 publisher が無ければ空リスト。
        """
        return [
            {"publicKeyId": k.public_key_id, "publicKey": k.public_key}
            for k in self._backend.get_publisher_keys(publisher)
        ]

    # --- 公開鍵登録(発行者認証必須) ---

    def register_public_key(
        self, token: str, public_key_id: str, public_key_b64: str
    ) -> dict[str, Any]:
        """発行者の ed25519 公開鍵を登録する。署名検証(publish)はこの鍵で行う。"""
        publisher = self._authenticate(token)
        if not public_key_id or not public_key_id.strip():
            raise RegistryValidationError("publicKeyId は非空でなければならない")
        raw = self._decode_public_key(public_key_b64)
        self._backend.register_key(
            publisher,
            PublisherKey(publicKeyId=public_key_id, publicKey=public_key_b64),
        )
        return {
            "publisher": publisher,
            "publicKeyId": public_key_id,
            "publicKeyLength": len(raw),
        }

    @staticmethod
    def _decode_public_key(public_key_b64: str) -> bytes:
        try:
            raw = base64.b64decode(public_key_b64, validate=True)
        except (binascii.Error, ValueError) as e:
            raise RegistryValidationError(f"publicKey は base64 でなければならない: {e}") from e
        if len(raw) != _ED25519_PUBLIC_KEY_LEN:
            raise RegistryValidationError(
                f"publicKey は {_ED25519_PUBLIC_KEY_LEN} バイトの ed25519 raw 公開鍵が必要"
            )
        return raw

    def _authenticate(self, token: str) -> str:
        publisher = self._auth.authenticate(token)
        if publisher is None:
            raise RegistryAuthError("発行者トークンが無効")
        return publisher

    # --- publish(発行者認証＋署名検証) ---

    def publish(self, token: str, manifest_data: dict[str, Any]) -> dict[str, Any]:
        """manifest を検証・署名確認のうえバックエンドへ保存し index を更新する。"""
        # 1. 発行者認証。
        publisher = self._authenticate(token)

        # 2. manifest 検証(PLG-01)。
        try:
            manifest = validate_manifest(manifest_data)
        except ManifestError as e:
            raise RegistryValidationError(f"manifest が不正: {e}") from e

        # 3. 発行者一致(他 publisher のなりすまし防止)。認可失敗は専用例外(API 層で 403)。
        if manifest.publisher != publisher:
            raise RegistryForbiddenError(
                f"manifest.publisher '{manifest.publisher}' は認証発行者 '{publisher}' と一致しない"
            )

        # 4. 無署名 publish の拒否(受け入れ条件)。
        if manifest.signature is None:
            raise RegistryValidationError("無署名の manifest は publish できない(署名必須)")
        signature = manifest.signature

        # 5. 公開鍵を引く(事前に register_public_key で登録が必要)。
        key = self._backend.get_public_key(publisher, signature.public_key_id)
        if key is None:
            raise RegistryValidationError(
                f"署名鍵 '{signature.public_key_id}' は未登録"
                f"(発行者 '{publisher}' の公開鍵を先に登録すること)"
            )
        # 6. ed25519 署名検証(改ざん/鍵不一致は False)。
        public_key_raw = self._decode_public_key(key.public_key)
        if not verify_signature(manifest, public_key_raw):
            raise RegistryValidationError("署名検証に失敗した(改ざん、または鍵不一致)")

        # 7. 成果物・sha・パスを確定しバックエンドへ原子的に追加(版の不変性は backend が担保)。
        artifact = _manifest_bytes(manifest)
        digest = hashlib.sha256(artifact).hexdigest()
        object_path = _artifact_path(manifest.id, manifest.version, digest)
        entry = IndexEntry(
            id=manifest.id,
            version=manifest.version,
            kind=manifest.kind,
            name=manifest.name,
            description=manifest.description,
            publisher=manifest.publisher,
            tags=list(manifest.tags),
            objectPath=object_path,
            sha256=digest,
            publicKeyId=signature.public_key_id,
            publishedAt=_utcnow_iso(),
            lifecycle=LIFECYCLE_ACTIVE,
            downloadCount=0,
        )
        self._backend.add_version(entry, artifact)
        return entry.model_dump(by_alias=True)

    # --- MKT-02 拡張: 評価 / 版ライフサイクル ---

    def rate_plugin(
        self, token: str, plugin_id: str, score: int, comment: str = ""
    ) -> dict[str, Any]:
        """プラグイン(id 単位)へ評価を登録/更新する(認証発行者を rater とする。1 rater 1 件)。

        MVP では消費者アイデンティティ統合(IAM)が未整備のため、rater は発行者トークン認証で代替する
        (匿名スパムを防ぎつつ既存認証を再利用)。score は 1〜5 の整数。
        """
        rater = self._authenticate(token)
        if isinstance(score, bool) or not isinstance(score, int):
            raise RegistryValidationError("score は整数でなければならない")
        if not (MIN_RATING <= score <= MAX_RATING):
            raise RegistryValidationError(
                f"score は {MIN_RATING}〜{MAX_RATING} の整数でなければならない"
            )
        comment = (comment or "").strip()
        if not self._backend.versions(plugin_id):
            raise RegistryNotFoundError(f"{plugin_id} は存在しない(評価できない)")
        self._backend.add_rating(plugin_id, rater, score, comment)
        return self._ratings_summary(plugin_id)

    def get_ratings(self, plugin_id: str) -> dict[str, Any]:
        """プラグインの評価集計(件数・平均・直近コメント)を返す。

        存在しない plugin は 404(get/download/rate と方針を揃える。無評価の既存 plugin と区別する)。
        """
        if not self._backend.versions(plugin_id):
            raise RegistryNotFoundError(f"{plugin_id} は存在しない")
        return self._ratings_summary(plugin_id)

    def _ratings_summary(self, plugin_id: str) -> dict[str, Any]:
        summary: RatingSummary = self._backend.get_ratings(plugin_id)
        return summary.to_dict()

    def set_lifecycle(
        self, token: str, plugin_id: str, version: str, state: str
    ) -> dict[str, Any]:
        """版ライフサイクルを設定する(所有発行者のみ)。active/deprecated/yanked。"""
        publisher = self._authenticate(token)
        if state not in LIFECYCLE_STATES:
            raise RegistryValidationError(
                f"lifecycle は {LIFECYCLE_STATES} のいずれかでなければならない: {state!r}"
            )
        entry = self._backend.find(plugin_id, version)
        if entry is None:
            raise RegistryNotFoundError(f"{plugin_id}@{version} は存在しない")
        if entry.publisher != publisher:
            raise RegistryForbiddenError(
                f"{plugin_id} の所有発行者 '{entry.publisher}' のみライフサイクルを変更できる"
            )
        updated = self._backend.set_lifecycle(plugin_id, version, state)
        return updated.model_dump(by_alias=True)
