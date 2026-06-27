"""ADB バックエンド: 中央レジストリ μService 本体(MKT-02)。

PLG-04 の Object Storage + index.json を ADB(`jetuse_core.db`)へ昇格した `RegistryBackend` 実装。
版/発行者公開鍵/評価/DL 数/版ライフサイクルを ADB に持ち、検索は SQL で行う。成果物(manifest 全文)は
同じ行の CLOB に保持し、μService を ADB 自己完結にする(E2E は loop ADB のみで成立する)。

原子性は DB 制約に委ねる:
  - 版の不変性: (plugin_id, version) PK。再 publish の INSERT は一意制約違反→RegistryConflictError。
  - DL 数: 行ロックの `UPDATE ... SET download_count = download_count + 1 ... RETURNING`(原子加算)。
  - 評価: (plugin_id, rater) PK への MERGE(upsert。1 rater 1 件)。

スキーマ DDL は `packages/api/jetuse_core/migrations/022_plugin_registry.sql`。
"""

from __future__ import annotations

import hashlib
import json

from jetuse_core.db import connect

from .backend import (
    Rating,
    RatingSummary,
    check_comment_storage_limit,
    check_entry_storage_limits,
    check_key_storage_limits,
    check_principal_storage_limit,
)
from .errors import (
    RegistryConflictError,
    RegistryNotFoundError,
    RegistryStorageError,
)
from .index import LIFECYCLE_STATES, IndexEntry, PublisherKey

#: SELECT で取り出す版エントリの列順(行→IndexEntry 変換と一対一)。
_ENTRY_COLS = (
    "plugin_id, version, kind, name, description, publisher, tags, "
    "object_path, sha256, public_key_id, published_at, lifecycle, download_count"
)

#: Oracle の一意制約違反(再 publish / 同時登録)エラーコード。
_ORA_UNIQUE_VIOLATION = 1


def _is_unique_violation(exc: Exception) -> bool:
    """oracledb の DatabaseError が一意制約違反(ORA-00001)かを判定する。"""
    err = getattr(exc, "args", None)
    if err:
        info = getattr(err[0], "code", None)
        if info == _ORA_UNIQUE_VIOLATION:
            return True
    return "ORA-00001" in str(exc)


def _like_escape(s: str) -> str:
    r"""LIKE の特殊文字(\ % _)をエスケープする(ESCAPE '\' と併用)。

    バックスラッシュを先にエスケープしてから %/_ を処理する(二重エスケープ防止)。
    """
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _parse_tags(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        val = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return [str(t) for t in val] if isinstance(val, list) else []


def _row_to_entry(r) -> IndexEntry:
    return IndexEntry(
        id=r[0],
        version=r[1],
        kind=r[2],
        name=r[3],
        description=r[4] or "",
        publisher=r[5],
        tags=_parse_tags(r[6]),
        objectPath=r[7],
        sha256=r[8],
        publicKeyId=r[9],
        publishedAt=r[10],
        lifecycle=r[11],
        downloadCount=int(r[12]),
    )


class AdbBackend:
    """ADB を保存層とする `RegistryBackend`(μService 本体)。"""

    # --- 読取 ---

    def list_entries(self) -> list[IndexEntry]:
        with connect() as conn:
            cur = conn.cursor()
            cur.execute(
                f"SELECT {_ENTRY_COLS} FROM registry_plugins "
                f"ORDER BY plugin_id, published_at"
            )
            return [_row_to_entry(r) for r in cur.fetchall()]

    def search(
        self,
        q: str | None = None,
        *,
        kind: str | None = None,
        tag: str | None = None,
    ) -> list[IndexEntry]:
        # DB 検索: kind 等値・q は id/name/desc 部分一致(大小無視)・tag は JSON 配列の要素一致。
        # LIKE のワイルドカード(% _ \)は検索値側をエスケープし ESCAPE '\' を付ける(InMemory/Index の
        # 部分一致/完全一致と挙動を揃え、入力の % _ が誤ってワイルドカードにならないようにする)。
        where: list[str] = []
        binds: dict[str, str] = {}
        if kind is not None:
            where.append("kind = :kind")
            binds["kind"] = kind
        if q:
            where.append(
                "(LOWER(plugin_id) LIKE :q ESCAPE '\\' OR LOWER(name) LIKE :q ESCAPE '\\' "
                "OR LOWER(description) LIKE :q ESCAPE '\\')"
            )
            binds["q"] = f"%{_like_escape(q.lower().strip())}%"
        if tag is not None:
            # tags は JSON 配列文字列(ensure_ascii=False 保存)。要素を "tag" 形でくくって一致させる
            # (非 ASCII タグもそのまま一致)。タグ値のワイルドカード文字を正規化(エスケープ)する。
            where.append("tags LIKE :tag ESCAPE '\\'")
            # 保存時と同じ JSON エンコード(json.dumps がクオート・バックスラッシュ等を正準化)した
            # 要素文字列を LIKE 値にし、LIKE 特殊文字をエスケープする(任意文字のタグでも一致)。
            binds["tag"] = "%" + _like_escape(json.dumps(tag, ensure_ascii=False)) + "%"
        sql = f"SELECT {_ENTRY_COLS} FROM registry_plugins"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY plugin_id, published_at"
        with connect() as conn:
            cur = conn.cursor()
            cur.execute(sql, **binds)
            return [_row_to_entry(r) for r in cur.fetchall()]

    def versions(self, plugin_id: str) -> list[IndexEntry]:
        with connect() as conn:
            cur = conn.cursor()
            cur.execute(
                f"SELECT {_ENTRY_COLS} FROM registry_plugins "
                f"WHERE plugin_id = :id ORDER BY published_at",
                id=plugin_id,
            )
            return [_row_to_entry(r) for r in cur.fetchall()]

    def find(self, plugin_id: str, version: str) -> IndexEntry | None:
        with connect() as conn:
            cur = conn.cursor()
            cur.execute(
                f"SELECT {_ENTRY_COLS} FROM registry_plugins "
                f"WHERE plugin_id = :id AND version = :v",
                id=plugin_id,
                v=version,
            )
            row = cur.fetchone()
            return _row_to_entry(row) if row else None

    def read_artifact(self, entry: IndexEntry) -> bytes:
        with connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT manifest FROM registry_plugins "
                "WHERE plugin_id = :id AND version = :v",
                id=entry.id,
                v=entry.version,
            )
            row = cur.fetchone()
        if row is None or row[0] is None:
            raise RegistryStorageError(
                f"index に在るが成果物が欠落している(保存層の不整合): {entry.id}@{entry.version}"
            )
        # fetch_lobs=False(db.py)により CLOB は str で返る。
        data = row[0].encode("utf-8") if isinstance(row[0], str) else bytes(row[0])
        if hashlib.sha256(data).hexdigest() != entry.sha256:
            raise RegistryStorageError(
                f"成果物の sha256 が index と不一致(破損/改ざんの疑い): {entry.id}@{entry.version}"
            )
        return data

    def get_public_key(self, publisher: str, public_key_id: str) -> PublisherKey | None:
        with connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT public_key_id, public_key FROM registry_publisher_keys "
                "WHERE publisher = :p AND public_key_id = :k",
                p=publisher,
                k=public_key_id,
            )
            row = cur.fetchone()
            if row is None:
                return None
            return PublisherKey(publicKeyId=row[0], publicKey=row[1])

    def get_publisher_keys(self, publisher: str) -> list[PublisherKey]:
        with connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT public_key_id, public_key FROM registry_publisher_keys "
                "WHERE publisher = :p ORDER BY public_key_id",
                p=publisher,
            )
            return [PublisherKey(publicKeyId=r[0], publicKey=r[1]) for r in cur.fetchall()]

    # --- 書込(原子的) ---

    def register_key(self, publisher: str, key: PublisherKey) -> None:
        # (publisher, public_key_id) は不変。同一鍵の再登録は冪等、別鍵への差し替えは 409。
        check_principal_storage_limit("publisher", publisher)  # publisher は VARCHAR2(255)。
        check_key_storage_limits(key)  # カラム幅超過は 422(ORA-12899=500 を防ぐ)。
        with connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT public_key FROM registry_publisher_keys "
                "WHERE publisher = :p AND public_key_id = :k",
                p=publisher,
                k=key.public_key_id,
            )
            row = cur.fetchone()
            if row is not None:
                if row[0] != key.public_key:
                    raise RegistryConflictError(
                        f"公開鍵 '{key.public_key_id}' は登録済みで別の鍵に差し替えできない"
                        f"(鍵 ID は不変。新しい鍵は別の publicKeyId で登録すること)"
                    )
                return  # 同一鍵=冪等な再登録。
            try:
                cur.execute(
                    "INSERT INTO registry_publisher_keys (publisher, public_key_id, public_key) "
                    "VALUES (:p, :k, :pk)",
                    p=publisher,
                    k=key.public_key_id,
                    pk=key.public_key,
                )
                conn.commit()
            except Exception as e:
                conn.rollback()
                if _is_unique_violation(e):
                    # 並行登録で先に入った。同一鍵なら冪等成功、別鍵なら 409。
                    existing = self.get_public_key(publisher, key.public_key_id)
                    if existing is not None and existing.public_key == key.public_key:
                        return
                    raise RegistryConflictError(
                        f"公開鍵 '{key.public_key_id}' は登録済みで別の鍵に差し替えできない"
                    ) from e
                raise

    def add_version(self, entry: IndexEntry, artifact: bytes) -> None:
        # (plugin_id, version) PK で再 publish の INSERT は一意制約違反→409(版不変を DB が担保)。
        check_entry_storage_limits(entry)  # カラム幅超過は 422(ORA-12899=500 を防ぐ)。
        import oracledb

        manifest_text = artifact.decode("utf-8")
        with connect() as conn:
            cur = conn.cursor()
            try:
                # manifest は CLOB。oracledb が長い str を VARCHAR2 扱い(ORA-01461)にせぬよう
                # CLOB 型を明示する(大きな sample-app/connector manifest も確実に保存)。
                cur.setinputsizes(manifest=oracledb.DB_TYPE_CLOB)
                cur.execute(
                    "INSERT INTO registry_plugins ("
                    "plugin_id, version, kind, name, description, publisher, tags, "
                    "object_path, sha256, public_key_id, published_at, lifecycle, "
                    "download_count, manifest) VALUES ("
                    ":id, :v, :kind, :name, :descr, :pub, :tags, :opath, :sha, :kid, "
                    ":pat, :life, :dc, :manifest)",
                    id=entry.id,
                    v=entry.version,
                    kind=entry.kind,
                    name=entry.name,
                    descr=entry.description,
                    pub=entry.publisher,
                    tags=json.dumps(list(entry.tags), ensure_ascii=False),
                    opath=entry.object_path,
                    sha=entry.sha256,
                    kid=entry.public_key_id,
                    pat=entry.published_at,
                    life=entry.lifecycle,
                    dc=entry.download_count,
                    manifest=manifest_text,
                )
                conn.commit()
            except Exception as e:
                conn.rollback()
                if _is_unique_violation(e):
                    raise RegistryConflictError(
                        f"{entry.id}@{entry.version} は既に publish 済み(版は不変)"
                    ) from e
                raise

    # --- MKT-02 拡張 ---

    def record_download(self, plugin_id: str, version: str) -> int | None:
        import oracledb

        with connect() as conn:
            cur = conn.cursor()
            out = cur.var(oracledb.NUMBER)
            cur.execute(
                "UPDATE registry_plugins SET download_count = download_count + 1 "
                "WHERE plugin_id = :id AND version = :v "
                "RETURNING download_count INTO :out",
                id=plugin_id,
                v=version,
                out=out,
            )
            if cur.rowcount == 0:
                conn.rollback()
                return None
            conn.commit()
            val = out.getvalue()
            # RETURNING は更新行ごとに list を返す(1 行更新なので [count])。
            count = val[0] if isinstance(val, list) else val
            return int(count)

    def set_lifecycle(self, plugin_id: str, version: str, state: str) -> IndexEntry:
        if state not in LIFECYCLE_STATES:
            raise RegistryStorageError(f"未知のライフサイクル状態: {state!r}")
        with connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE registry_plugins SET lifecycle = :s "
                "WHERE plugin_id = :id AND version = :v",
                s=state,
                id=plugin_id,
                v=version,
            )
            if cur.rowcount == 0:
                conn.rollback()
                raise RegistryNotFoundError(f"{plugin_id}@{version} は存在しない")
            conn.commit()
        updated = self.find(plugin_id, version)
        if updated is None:  # 直前に更新したので通常起きない(並行削除はこの MVP では無い)。
            raise RegistryStorageError(f"{plugin_id}@{version} の更新後再取得に失敗")
        return updated

    def add_rating(self, plugin_id: str, rater: str, score: int, comment: str) -> None:
        # (plugin_id, rater) への upsert(1 rater 1 件)。MERGE で原子的に挿入/更新する。
        check_principal_storage_limit("rater", rater)  # rater は VARCHAR2(255)。
        check_comment_storage_limit(comment)  # カラム幅超過は 422。
        with connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "MERGE INTO registry_ratings t "
                "USING (SELECT :id AS plugin_id, :rater AS rater FROM dual) s "
                "ON (t.plugin_id = s.plugin_id AND t.rater = s.rater) "
                "WHEN MATCHED THEN UPDATE SET t.score = :score, t.comment_text = :cmt, "
                "t.created_at = SYSTIMESTAMP "
                "WHEN NOT MATCHED THEN INSERT (plugin_id, rater, score, comment_text) "
                "VALUES (:id, :rater, :score, :cmt)",
                id=plugin_id,
                rater=rater,
                score=score,
                cmt=comment,
            )
            conn.commit()

    def get_ratings(self, plugin_id: str) -> RatingSummary:
        with connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT rater, score, comment_text, "
                "TO_CHAR(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS') "
                "FROM registry_ratings WHERE plugin_id = :id "
                "ORDER BY created_at DESC",
                id=plugin_id,
            )
            rows = cur.fetchall()
        ratings = [
            Rating(rater=r[0], score=int(r[1]), comment=r[2] or "", created_at=r[3] or "")
            for r in rows
        ]
        count = len(ratings)
        average = round(sum(r.score for r in ratings) / count, 2) if count else None
        return RatingSummary(
            plugin_id=plugin_id, count=count, average=average, ratings=ratings
        )


def build_from_env() -> AdbBackend:
    """ADB バックエンドを構築する(本番/手動 E2E 用)。

    接続は `jetuse_core.db`(ADB ウォレット・mTLS)が `~/.oci/config` か resource_principal で行う。
    スキーマ(表)は `python -m jetuse_core.migrate` で 022_plugin_registry.sql を適用済みであること。
    OCID・認証値は .env 管理でリポジトリにコミットしない。
    """
    return AdbBackend()
