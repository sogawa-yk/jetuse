"""owner キー導出の単一ヘルパー + 外部名 file_key 導出(specs/18 §3.1・§3.2.1)。

キー空間の分離は認証段階の形式拒否ではなく「単射エスケープ」で構造保証する:
user 経路の owner キーは必ず user_owner_key() を通し、sub が予約接頭辞
(`demo_` / `sub_`)で始まる場合のみ `sub_` を前置する(実在の sub には no-op =
既存データと互換)。demo キーは常にサーバ生成の `demo_<uuid>`。

外部リソース名(OCI Files filename / Object Storage object 名)は file_key() が正本。
upload・起動時 reconcile・個別 DELETE・demo DELETE・E2E fixture すべてがこれを共用する
(specs/18 §3.1 — 箇所ごとの規則差は「作成側と削除側で見つからない」事故になる)。
"""

import hashlib
import logging

logger = logging.getLogger("jetuse.owner_keys")

RESERVED_PREFIXES = ("demo_", "sub_")
_MAX_OWNER_BYTES = 255  # owner_sub 列は VARCHAR2(255)

# 符号化導入マーカー(specs/18 §3.2.1)。schema_migrations に記録され、以後 preflight はスキップ
OWNER_KEY_MARKER = "owner_key_v1"


def user_owner_key(sub: str) -> str:
    """user 経路の owner キー(単射・決定的)。永続資産の owner はすべてこれを通す。

    予約接頭辞でない sub は no-op(既存データと互換)。前置で 255 バイトを超える場合は
    `sub_h_<sha1 40hex>` 形式(決定的・長さ有界 — specs/18 §3.2.1 長さの境界)。
    """
    if not sub.startswith(RESERVED_PREFIXES):
        return sub
    escaped = f"sub_{sub}"
    if len(escaped.encode()) > _MAX_OWNER_BYTES:
        return f"sub_h_{hashlib.sha1(sub.encode()).hexdigest()}"
    return escaped


def owner_hash(owner_key: str) -> str:
    """owner キーの sha1 40hex(小文字)。store metadata / 外部名 / 原本 prefix で共用。

    固定長の完全ハッシュ(切詰めなし): raw キーは 255 バイトまでありうるが、これなら
    全経路で一意・64 文字内(specs/18 §3.2 手順 3b)。
    """
    return hashlib.sha1(owner_key.encode()).hexdigest()


def normalize_ext(filename: str) -> str:
    """検証済みファイル名から正規化拡張子(小文字・ドットなし)を取り出す。"""
    dot = filename.rfind(".")
    return filename[dot + 1:].lower() if dot >= 0 else "bin"


def file_key(owner_key: str, reservation_id: str, ext: str) -> str:
    """OCI Files の filename(specs/18 §3.1 の導出関数)。ext はドットなし正規化拡張子。

    OCI Files は file_id 参照のため owner セグメントは常に完全 sha1 でよい(既存資産の
    参照は id 経由で不変。命名は一意性のみが要件)。
    """
    return f"{owner_hash(owner_key)}/{reservation_id}.{ext.lstrip('.').lower()}"


def _storage_seg(owner_key: str) -> str:
    """Object Storage 原本 / Select AI 索引 location の owner セグメント。

    demo は完全 sha1(箱の越境防止 — specs/18 §3.1)。user は main 互換の raw owner:
    既存 user の Select AI 索引 location は main の `rag/<owner>` に固定されており(索引名は
    8hex で不変)、原本だけ `rag/<sha1>` へ移すと更新後の新規アップロードが既存索引に取り込ま
    れない(codex review-12 B002)。user 側の完全ハッシュ化は main バックポート課題(residual)。
    """
    return owner_hash(owner_key) if is_demo_namespace(owner_key) else owner_key


def original_object_name(owner_key: str, reservation_id: str, ext: str) -> str:
    """Object Storage 原本の object 名(basename は file_key と同じ <rid>.<ext>)。"""
    ext = ext.lstrip(".").lower()
    return f"rag/{_storage_seg(owner_key)}/{reservation_id}.{ext}"


def original_prefix(owner_key: str) -> str:
    """原本の owner 単位 prefix(削除時のページネーション全列挙に使う)。"""
    return f"rag/{_storage_seg(owner_key)}/"


def is_demo_namespace(owner_key: str) -> bool:
    return owner_key.startswith("demo_")


# --- 符号化導入 preflight(specs/18 §3.2.1 — 分類 → 人間承認 → クリーンアップ → マーカー) ---

_preflight_ok: bool | None = None


class OwnerKeyPreflightError(Exception):
    """予約接頭辞の既存行が未分類のまま残っている(fail-closed 503)。"""


def _reserved_rows(cur) -> list[dict]:
    """owner キーを持つ各表の予約接頭辞行を全列挙する(分類リスト)。"""
    rows: list[dict] = []
    targets = [
        ("RAG_STORES", "owner_sub"),
        ("RAG_FILES", "owner_sub"),
        ("JETUSE_DATASETS", "owner_sub"),
        ("CONVERSATIONS", "owner_sub"),
    ]
    for table, col in targets:
        cur.execute(
            "SELECT COUNT(*) FROM user_tables WHERE table_name = :t", t=table
        )
        if cur.fetchone()[0] == 0:  # JETUSE_DATASETS は実行時作成のため無いことがある
            continue
        cur.execute(
            f"SELECT {col} FROM {table} WHERE {col} LIKE 'demo\\_%' ESCAPE '\\' "
            f"OR {col} LIKE 'sub\\_%' ESCAPE '\\'"
        )
        for (owner,) in cur.fetchall():
            rows.append({"table": table, "owner": owner})
    return rows


def preflight_classification(cur) -> list[dict]:
    """分類リストを返す(demos 表との対応を手掛かりとして付与)。承認資料用。"""
    rows = _reserved_rows(cur)
    cur.execute("SELECT id FROM demos")
    demo_ids = {r[0] for r in cur.fetchall()}
    for r in rows:
        o = r["owner"]
        r["matches_demo_row"] = o.startswith("demo_") and o[5:] in demo_ids
    return rows


def owner_key_gate() -> None:
    """符号化導入前の予約接頭辞行が残っている間、該当経路を fail-closed にするゲート。

    マーカー(owner_key_v1)記録済みなら以後スキップ(プロセス内キャッシュ)。
    行ゼロなら自動でマーカーを記録して通す(fresh 環境の通常経路)。
    """
    global _preflight_ok
    if _preflight_ok:
        return
    from .db import connect

    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = :v",
            v=OWNER_KEY_MARKER,
        )
        if cur.fetchone()[0]:
            _preflight_ok = True
            return
        leftovers = _reserved_rows(cur)
        if leftovers:
            # user 資産と分類された行が残る限りマーカーは記録されない(ADR 解決が先)
            logger.error(
                "owner key preflight: %d reserved-prefix rows need classification",
                len(leftovers),
            )
            raise OwnerKeyPreflightError(
                f"{len(leftovers)} reserved-prefix owner rows need human classification"
            )
        try:
            cur.execute(
                "INSERT INTO schema_migrations(version) VALUES (:v)", v=OWNER_KEY_MARKER
            )
            conn.commit()
        except Exception as e:  # noqa: BLE001
            # ORA-00001(他プロセスが記録済み)だけ成功扱い。権限不足・DB障害・ローリング
            # 配備中の旧 writer 等でゲートを誤って開けない(codex review-2 major)。
            conn.rollback()
            if "ORA-00001" not in str(e):
                raise OwnerKeyPreflightError(
                    f"owner key marker persist failed: {str(e)[:200]}"
                ) from e
        # マーカーの実在を再確認してからゲートを開く(INSERT 例外を握り潰さない)
        cur.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = :v", v=OWNER_KEY_MARKER
        )
        if not cur.fetchone()[0]:
            raise OwnerKeyPreflightError("owner key marker not persisted")
        _preflight_ok = True
