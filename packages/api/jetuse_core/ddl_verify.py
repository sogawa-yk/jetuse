"""実行時 DDL の冪等性を「ORA コード無視」でなく辞書検証で担保する最小ヘルパー。

specs/18 §3.2 手順 2(_ensure_meta の state 列)・§3.1(ledger の完全 DDL)が共用する。
期待と異なる形(型・長さ・NULL 可否・制約条件)は停止して人間対応(RuntimeError)。
"""

from typing import Any


class DdlShapeMismatch(RuntimeError):
    """同名で形の違うオブジェクトが存在する(人間対応が必要)。"""


def _norm_ws(s: str) -> str:
    return " ".join(s.split())


def table_exists(cur, table: str) -> bool:
    cur.execute("SELECT COUNT(*) FROM user_tables WHERE table_name = :t", t=table)
    return cur.fetchone()[0] > 0


def column_state(cur, table: str, column: str) -> dict[str, Any] | None:
    """列の現状(なければ None)。data_type / char 長 / NULL 可否 / DEFAULT を返す。"""
    cur.execute(
        "SELECT data_type, char_length, char_used, nullable, data_default "
        "FROM user_tab_columns WHERE table_name = :t AND column_name = :c",
        t=table, c=column,
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        "data_type": row[0], "char_length": row[1] or None, "char_used": row[2] or None,
        "nullable": row[3],
        "data_default": str(row[4]).strip() if row[4] is not None else None,
    }


def verify_column(cur, table: str, column: str, *, data_type: str,
                  char_length: int | None = None, char_used: str | None = None,
                  nullable: str | None = None) -> bool:
    """列が期待形で存在すれば True、無ければ False、形違いは停止。

    nullable=None は比較しない(段階適用の途中形を許すため)。
    """
    got = column_state(cur, table, column)
    if got is None:
        return False
    checks = [("data_type", data_type)]
    if char_length is not None:
        checks += [("char_length", char_length), ("char_used", char_used)]
    if nullable is not None:
        checks.append(("nullable", nullable))
    for key, want in checks:
        if got[key] != want:
            raise DdlShapeMismatch(
                f"{table}.{column}: {key} = {got[key]!r} (期待 {want!r})。"
                "同名で形の違う列が存在するため停止(人間対応が必要)"
            )
    return True


def check_constraint_state(cur, table: str, condition: str) -> str | None:
    """指定条件の CHECK 制約の制約名を返す(なければ None)。空白正規化で照合。"""
    cur.execute(
        "SELECT constraint_name, search_condition FROM user_constraints "
        "WHERE table_name = :t AND constraint_type = 'C'",
        t=table,
    )
    for name, cond in cur.fetchall():
        if cond and _norm_ws(cond) == _norm_ws(condition):
            return name
    return None
