"""MEM-01 着手前の実機調査: ADB 26ai に「Agent Memory」の API があるかを辞書ビューで確かめる。

読み取り専用（辞書ビューの SELECT のみ。DDL も DML も行わない）。

**「無い」を主張するための調査なので、問い合わせの失敗と 0 件を厳密に分ける。**
必須の問い合わせが 1 つでも失敗したら（権限不足・SQL 非互換など）、エラーを記録して
**非ゼロ終了する**。失敗を握り潰して「0 件だった＝機能が無い」と読める出力を残さない。

実行:
  SPIKE_SCHEMA_PREFIX=JETUSE_MEM01 SPIKE_HOME=/tmp/jetuse-mem01 \
    .venv/bin/python spikes/mem01/probe_agent_memory.py
"""

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "spikes" / "ragm02"))
sys.path.insert(0, str(ROOT / "packages" / "api"))

import common  # noqa: E402

# (名前, SQL, 結論に必須か)。必須のものが失敗したら調査自体を失敗にする。
QUERIES: list[tuple[str, str, bool]] = [
    ("version", "SELECT banner_full FROM v$version", True),
    ("compatible", "SELECT name, value FROM v$parameter WHERE name = 'compatible'", False),
    # 「記憶に相当する DB オブジェクトが無い」の直接の根拠
    ("objects_memory",
     "SELECT owner, object_name, object_type FROM all_objects "
     "WHERE object_name LIKE '%MEMOR%' AND object_type IN "
     "('PACKAGE','PROCEDURE','FUNCTION','TYPE','VIEW','TABLE','SYNONYM') "
     "ORDER BY owner, object_name FETCH FIRST 100 ROWS ONLY", True),
    ("objects_agent",
     "SELECT owner, object_name, object_type FROM all_objects "
     "WHERE object_name LIKE '%AI_AGENT%' ORDER BY owner, object_name "
     "FETCH FIRST 100 ROWS ONLY", True),
    ("packages_ai",
     "SELECT owner, object_name, object_type FROM all_objects "
     "WHERE object_type IN ('PACKAGE','SYNONYM') AND ("
     " object_name LIKE 'DBMS_CLOUD_AI%' OR object_name LIKE 'DBMS_AI%' "
     " OR object_name LIKE 'DBMS_VECTOR%' OR object_name LIKE 'DBMS_MCP%') "
     "ORDER BY object_name", True),
    ("dbms_cloud_ai_agent_subprograms",
     "SELECT DISTINCT procedure_name FROM all_procedures "
     "WHERE object_name = 'DBMS_CLOUD_AI_AGENT' ORDER BY 1", True),
    ("dbms_cloud_ai_subprograms",
     "SELECT DISTINCT procedure_name FROM all_procedures "
     "WHERE object_name = 'DBMS_CLOUD_AI' ORDER BY 1", True),
    ("dbms_vector_chain_subprograms",
     "SELECT DISTINCT procedure_name FROM all_procedures "
     "WHERE object_name = 'DBMS_VECTOR_CHAIN' ORDER BY 1", False),
    ("dict_views_ai",
     "SELECT table_name FROM dictionary WHERE table_name LIKE '%AI_AGENT%' "
     "OR table_name LIKE '%MEMOR%' OR table_name LIKE '%CONVERSATION%' ORDER BY 1", True),
    # パッケージ仕様の本文（非 wrap なら全文読める）に記憶関連の語があるか
    ("source_lines_available",
     "SELECT name, COUNT(*) AS lines FROM all_source WHERE name IN "
     "('DBMS_CLOUD_AI','DBMS_CLOUD_AI_AGENT') AND type = 'PACKAGE' GROUP BY name", True),
    ("source_memory_grep",
     "SELECT name, line, text FROM all_source WHERE name IN "
     "('DBMS_CLOUD_AI','DBMS_CLOUD_AI_AGENT') AND type = 'PACKAGE' AND ("
     " UPPER(text) LIKE '%MEMOR%' OR UPPER(text) LIKE '%REMEMBER%' "
     " OR UPPER(text) LIKE '%RECALL%' OR UPPER(text) LIKE '%LONG-TERM%' "
     " OR UPPER(text) LIKE '%SHORT-TERM%' OR UPPER(text) LIKE '%PERSONA%') "
     "FETCH FIRST 100 ROWS ONLY", True),
]

# 「読めなかったから 0 件」ではないことを担保する対照。ここが 0 なら仕様本文を読めていない。
SANITY = {"source_lines_available": "パッケージ仕様を 1 行も読めていない"}


def main() -> int:
    conn = common.connect_admin()
    cur = conn.cursor()
    out: dict[str, object] = {}
    errors: list[str] = []
    for name, sql, required in QUERIES:
        try:
            cur.execute(sql)
            cols = [d[0].lower() for d in cur.description]
            rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
            out[name] = rows
            if name in SANITY and not rows:
                errors.append(f"{name}: {SANITY[name]}")
        except Exception as e:  # noqa: BLE001 — 失敗も結果として残すが、必須なら落とす
            detail = f"{type(e).__name__}: {str(e).splitlines()[0]}"
            out[name] = {"error": detail}
            if required:
                errors.append(f"{name}: {detail}")
    conn.close()
    out["errors"] = errors
    path = pathlib.Path("/tmp/mem01-probe-dictionary.json")
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(f"wrote {path}")
    if errors:
        print("必須の問い合わせが失敗した。この出力を「機能が無い」の根拠にしないこと:\n  "
              + "\n  ".join(errors), file=sys.stderr)
        return 1
    print("結論に必要な問い合わせはすべて成功した")
    return 0


if __name__ == "__main__":
    sys.exit(main())
