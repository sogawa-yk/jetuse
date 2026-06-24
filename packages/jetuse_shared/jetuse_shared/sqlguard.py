"""SQLサニタイズ(SELECT/WITH限定の多層ガード)。API/コンテナ共有 — jetuse_shared。

元実装: `jetuse_core/nl2sql.py::sanitize_sql`(SqlRejectedError) と
`agent-containers/agent_db.py::_sanitize`(ValueError)を一本化。

例外は jetuse_shared 固有の `SqlRejectedError` を送出する(ValueError サブクラスなので
従来 ValueError を catch していた呼び出し側も引き続き機能する)。API側は自前の
`SqlRejectedError` を別名で再エクスポートして後方互換を保つ。
"""

import re

_BANNED = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|CREATE|GRANT|REVOKE|TRUNCATE|"
    r"EXECUTE|CALL|LOCK|COMMIT|ROLLBACK)\b",
    re.I,
)


class SqlRejectedError(ValueError):
    """ガードで拒否したSQL。"""


def sanitize_sql(sql: str) -> str:
    """SELECT/WITH以外を拒否。コメント・セミコロン除去後に判定(SQL-02ガード)。"""
    cleaned = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    cleaned = re.sub(r"--[^\n]*", " ", cleaned)
    cleaned = cleaned.strip().rstrip(";").strip()
    if ";" in cleaned:
        raise SqlRejectedError("複数ステートメントは実行できません")
    if not cleaned.upper().startswith(("SELECT", "WITH")):
        raise SqlRejectedError("SELECT文のみ実行できます")
    if _BANNED.search(cleaned):
        raise SqlRejectedError("更新系キーワードを含むSQLは実行できません")
    return cleaned
