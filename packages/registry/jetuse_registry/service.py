"""中央レジストリのドメインサービス(list/search/get/download/publish)。

保存層(`ObjectStore`)と発行者認証(`PublisherAuthenticator`)に依存し、HTTP には依存しない。
`index.json` を正本として読み書きし、成果物(manifest 全文)を Object Storage に保存する。

publish の認可・検証フロー(無署名拒否を含む):
  1. 発行者トークンを authenticate(失敗→RegistryAuthError)。
  2. manifest を PLG-01 で検証(失敗→RegistryValidationError)。
  3. manifest.publisher が認証 publisher と一致(不一致→RegistryForbiddenError=403。なりすまし防止)。
  4. signature が存在(無ければ→RegistryValidationError = **無署名 publish 拒否**)。
  5. signature.publicKeyId に対応する登録公開鍵を引く(未登録→RegistryValidationError)。
  6. verify_signature が True(改ざん/不一致→RegistryValidationError)。
  7. (id, version) が未登録(既存→RegistryConflictError。版は不変)。
  8. 成果物を Object Storage に保存し、index.json を更新して書き戻す。
"""

from __future__ import annotations

import base64
import binascii
import datetime
import hashlib
from collections.abc import Callable
from typing import Any, TypeVar

from jetuse_core.plugins.manifest import (
    ManifestError,
    PluginManifest,
    validate_manifest,
    verify_signature,
)

from . import semver
from .errors import (
    RegistryAuthError,
    RegistryConflictError,
    RegistryError,
    RegistryForbiddenError,
    RegistryNotFoundError,
    RegistryStorageError,
    RegistryValidationError,
)
from .index import INDEX_OBJECT_NAME, IndexEntry, PublisherKey, RegistryIndex
from .publishers import PublisherAuthenticator
from .storage import IF_NONE_MATCH_ANY, ObjectStore, PreconditionFailed

#: ed25519 raw 公開鍵のバイト長。
_ED25519_PUBLIC_KEY_LEN = 32

#: index.json の楽観的更新リトライ上限(並行 publish 衝突時の読み直し回数)。
_MAX_INDEX_RETRIES = 8

_T = TypeVar("_T")


def _utcnow_iso() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def _artifact_path(plugin_id: str, version: str, digest: str) -> str:
    # id は namespace/name。Object Storage オブジェクト名にスラッシュ階層として展開する。
    # 成果物パスに sha256 を含める(content-addressed)。これにより publish が index 確定前に失敗して
    # 成果物だけ残っても(orphan)、同一 (id,version) を別内容で再 publish した際に内容の異なる
    # 成果物が同一パスへ衝突しない(orphan による版の汚染を防ぐ)。版の不変性は index の一意で担保。
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
    """Object Storage + index.json を保存層とする中央レジストリ。"""

    def __init__(self, store: ObjectStore, authenticator: PublisherAuthenticator) -> None:
        self._store = store
        self._auth = authenticator

    # --- index 読み書き ---

    def _load_index(self) -> RegistryIndex:
        """読取用に index を取得する(楽観ロック不要の参照系)。"""
        try:
            raw, _ = self._store.get_with_etag(INDEX_OBJECT_NAME)
        except KeyError:
            return RegistryIndex.empty()
        return RegistryIndex.from_bytes(raw)

    def _update_index(self, mutate: Callable[[RegistryIndex], _T]) -> _T:
        """index を read-modify-write で更新する(楽観的並行制御＋リトライ)。

        index を etag つきで読み、`mutate` で検証・変更し、`if_match`(既存)/
        `if_none_match=*`(新規)で条件付き保存する。保存までの間に他者が更新していれば
        `PreconditionFailed` を受け、index を読み直して `mutate` をやり直す
        (同時 publish での更新消失・不変性破りを防ぐ)。`mutate` が送出するドメイン例外
        (検証失敗・409 等)はリトライせずそのまま伝播する。
        """
        for _ in range(_MAX_INDEX_RETRIES):
            try:
                raw, etag = self._store.get_with_etag(INDEX_OBJECT_NAME)
                index = RegistryIndex.from_bytes(raw)
                exists = True
            except KeyError:
                index, etag, exists = RegistryIndex.empty(), None, False
            # 既存 index なのに etag が取れない=楽観ロックの前提が崩れる(空 if_match は永久衝突)。
            # 黙ってリトライ消尽させず、保存層エラーとして即座に表面化させる(原因診断のため)。
            if exists and not etag:
                raise RegistryStorageError(
                    "index.json の etag を取得できず安全に更新できない(保存層/レスポンス形状の異常)"
                )

            result = mutate(index)  # ドメイン検証はここで(失敗はリトライせず伝播)。
            payload = index.to_bytes()
            try:
                if exists:
                    self._store.put(
                        INDEX_OBJECT_NAME, payload,
                        content_type="application/json", if_match=etag,
                    )
                else:
                    self._store.put(
                        INDEX_OBJECT_NAME, payload,
                        content_type="application/json", if_none_match=IF_NONE_MATCH_ANY,
                    )
                return result
            except PreconditionFailed:
                continue  # 他者が先に更新 → 読み直してやり直す。
        raise RegistryError(
            "index.json の更新が並行更新の衝突で確定できなかった(リトライ上限超過)"
        )

    # --- 読取 API(公開) ---

    def list_plugins(self) -> list[dict[str, Any]]:
        """登録済みの全プラグイン版メタデータを返す(発行者鍵は含めない)。"""
        return self._load_index().public_summary()

    def search(
        self,
        q: str | None = None,
        *,
        kind: str | None = None,
        tag: str | None = None,
    ) -> list[dict[str, Any]]:
        """q(id/name/description 部分一致, 大小無視)・kind・tag で絞り込んだ一覧を返す。"""
        ql = q.lower().strip() if q else None
        results: list[dict[str, Any]] = []
        for e in self._load_index().plugins:
            if kind is not None and e.kind != kind:
                continue
            if tag is not None and tag not in e.tags:
                continue
            if ql:
                haystack = f"{e.id}\n{e.name}\n{e.description}".lower()
                if ql not in haystack:
                    continue
            results.append(e.model_dump(by_alias=True))
        return results

    def get(self, plugin_id: str, version: str | None = None) -> dict[str, Any]:
        """エントリ＋manifest 全文を返す。version 省略時は最新(semver precedence)を選ぶ。"""
        index = self._load_index()
        entry = self._resolve_entry(index, plugin_id, version)
        manifest = self._load_manifest(entry)
        return {
            "entry": entry.model_dump(by_alias=True),
            "manifest": manifest,
        }

    def download(self, plugin_id: str, version: str | None = None) -> tuple[bytes, IndexEntry]:
        """成果物(manifest 全文)のバイト列とエントリを返す。version 省略時は最新。"""
        index = self._load_index()
        entry = self._resolve_entry(index, plugin_id, version)
        return self._read_verified_artifact(entry), entry

    def _read_verified_artifact(self, entry: IndexEntry) -> bytes:
        """成果物を読み、index の sha256 と一致するか検証して返す。

        欠落=保存層の不整合(404 で隠さず表面化)。sha 不一致=破損/改ざんを取込側に渡さないため拒否。
        成果物パスは content-addressed だが、保存層の破損・取り違えを download/get 時にも自衛する。
        """
        try:
            data = self._store.get(entry.object_path)
        except KeyError as e:
            raise RegistryStorageError(
                f"index に在るが成果物が欠落している(保存層の不整合): {entry.object_path}"
            ) from e
        if hashlib.sha256(data).hexdigest() != entry.sha256:
            raise RegistryStorageError(
                f"成果物の sha256 が index と不一致(破損/改ざんの疑い): {entry.object_path}"
            )
        return data

    def _resolve_entry(
        self, index: RegistryIndex, plugin_id: str, version: str | None
    ) -> IndexEntry:
        if version is not None:
            entry = index.find(plugin_id, version)
            if entry is None:
                raise RegistryNotFoundError(f"{plugin_id}@{version} は存在しない")
            return entry
        candidates = index.versions(plugin_id)
        if not candidates:
            raise RegistryNotFoundError(f"{plugin_id} は存在しない")
        latest_version = semver.latest([c.version for c in candidates])
        # 同 version は publish で一意化済み。find で確実に 1 件取れる。
        entry = index.find(plugin_id, latest_version)
        assert entry is not None  # versions に在った version は必ず find できる。
        return entry

    def _load_manifest(self, entry: IndexEntry) -> dict[str, Any]:
        import json

        return json.loads(self._read_verified_artifact(entry).decode("utf-8"))

    def get_publisher_keys(self, publisher: str) -> list[dict[str, str]]:
        """発行者の登録済み公開鍵(publicKeyId + 公開鍵)を返す。取込側(PLG-03)の署名検証用。

        公開鍵は秘匿情報ではなく、取込クライアントが manifest.signature を検証するために必要
        (specs/16 §6 / ADR-0013: 「レジストリ取得の公開鍵」で検証)。read 系として無認証で公開する。
        該当 publisher が無ければ空リスト。
        """
        keys = self._load_index().publisher_keys.get(publisher, {})
        return [
            {"publicKeyId": k.public_key_id, "publicKey": k.public_key}
            for k in keys.values()
        ]

    # --- 公開鍵登録(発行者認証必須) ---

    def register_public_key(
        self, token: str, public_key_id: str, public_key_b64: str
    ) -> dict[str, Any]:
        """発行者の ed25519 公開鍵を登録する。署名検証(publish)はこの鍵で行う。"""
        publisher = self._auth.authenticate(token)
        if publisher is None:
            raise RegistryAuthError("発行者トークンが無効")
        if not public_key_id or not public_key_id.strip():
            raise RegistryValidationError("publicKeyId は非空でなければならない")
        raw = self._decode_public_key(public_key_b64)

        def _mutate(index: RegistryIndex) -> None:
            # (publisher, publicKeyId) は不変。過去に publish 済み manifest の検証可能性を
            # 壊さないため、同一鍵の冪等な再登録だけ許可し、異なる鍵での差し替えは 409 で拒否する。
            # 等価判定は **base64 文字列ではなくデコード後の鍵バイト列**で行う(表現ゆれで等価な鍵の
            # 再登録が誤って 409 になるのを避け、登録 API の冪等性を保つ)。
            existing = index.get_public_key(publisher, public_key_id)
            if existing is not None and _decode_key_or_none(existing.public_key) != raw:
                raise RegistryConflictError(
                    f"公開鍵 '{public_key_id}' は登録済みで別の鍵に差し替えできない"
                    f"(鍵 ID は不変。新しい鍵は別の publicKeyId で登録すること)"
                )
            index.register_key(
                publisher,
                PublisherKey(publicKeyId=public_key_id, publicKey=public_key_b64),
            )

        # 楽観的並行制御で index を更新(同時登録での消失を防ぐ)。
        self._update_index(_mutate)
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

    # --- publish(発行者認証＋署名検証) ---

    def publish(self, token: str, manifest_data: dict[str, Any]) -> dict[str, Any]:
        """manifest を検証・署名確認のうえ Object Storage へ保存し index を更新する。"""
        # 1. 発行者認証。
        publisher = self._auth.authenticate(token)
        if publisher is None:
            raise RegistryAuthError("発行者トークンが無効")

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

        # 成果物バイト列・sha・パスは index 非依存に確定できる(リトライしても同一)。
        artifact = _manifest_bytes(manifest)
        digest = hashlib.sha256(artifact).hexdigest()
        object_path = _artifact_path(manifest.id, manifest.version, digest)
        signature = manifest.signature  # None でないことは上で保証済み(型の絞り込み)。

        def _mutate(index: RegistryIndex) -> dict[str, Any]:
            # 5. 公開鍵を引く(事前に register_public_key で登録が必要)。
            key = index.get_public_key(publisher, signature.public_key_id)
            if key is None:
                raise RegistryValidationError(
                    f"署名鍵 '{signature.public_key_id}' は未登録"
                    f"(発行者 '{publisher}' の公開鍵を先に登録すること)"
                )
            # 6. ed25519 署名検証(改ざん/鍵不一致は False)。
            public_key_raw = self._decode_public_key(key.public_key)
            if not verify_signature(manifest, public_key_raw):
                raise RegistryValidationError("署名検証に失敗した(改ざん、または鍵不一致)")
            # 7. 版の不変性(既存版への再 publish を拒否)。並行 publish でも条件付き put で確定する。
            if index.find(manifest.id, manifest.version) is not None:
                raise RegistryConflictError(
                    f"{manifest.id}@{manifest.version} は既に publish 済み(版は不変)"
                )
            # 8. 成果物保存 + index 更新(publish 時に index.json を更新)。
            # object_path は content-addressed(sha 入り)なので、内容が違えばパスも違う=衝突しない。
            # if_none_match='*' で「不在のときだけ作成」し、既存時(=同一 sha=同一内容、自分の
            # リトライや冪等な再 publish)はそのまま続行する。
            # 異内容での衝突は理論上 sha256 衝突時のみ。その場合は 409 に倒す(防御的)。
            try:
                self._store.put(
                    object_path, artifact,
                    content_type="application/json", if_none_match=IF_NONE_MATCH_ANY,
                )
            except PreconditionFailed:
                existing = self._store.get(object_path)
                if existing != artifact:
                    raise RegistryConflictError(
                        f"{manifest.id}@{manifest.version} の成果物が別内容で既に存在(版は不変)"
                    ) from None
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
            )
            index.upsert_entry(entry)
            return entry.model_dump(by_alias=True)

        return self._update_index(_mutate)
