"""TOOL-03 の実環境デプロイ: run 固有スキーマへ全マイグレーションを適用する。

共有 loop ADB を再利用し、スキーマだけで隔離する(ADB は増やさない)。接続・所有台帳・
fail-closed ゲートは RAGM-02 の検証共通部(`spikes/ragm02/common.py`)をそのまま使う。
TOOL-03 は **DB スキーマを変えない**(`parameters` は既に CLOB)ので、ここで示すのは
「マイグレーションが冪等に通り、`HTTP_TOOLS.PARAMETERS` が入れ子 JSON を保持できる型である」
ことまで。

実行:
  env SPIKE_SCHEMA_PREFIX=JETUSE_TOOL03 SPIKE_HOME=/tmp/jetuse-tool03 \
      PYTHONPATH=spikes/ragm02:spikes/tool03:packages/api \
      .venv/bin/python spikes/tool03/deploy.py
"""

import os
import sys

from common import banner, connect_schema, prepare_env, require_schema, secret

SCHEMA = require_schema()


def use_task_schema() -> None:
    """`jetuse_core.db` の接続先をこの run のスキーマへ向ける(他タスクの資源に触れない)。"""
    prepare_env()
    os.environ["ADB_USER"] = SCHEMA
    os.environ["ADB_PASSWORD"] = secret("schema_password")
    os.environ["COMPARTMENT_OCID"] = os.environ["ADB_COMPARTMENT_OCID"]
    from jetuse_core.settings import get_settings

    get_settings.cache_clear()
    if get_settings().adb_user != SCHEMA:
        sys.exit(f"接続先スキーマが {get_settings().adb_user}。E2E は {SCHEMA} でしか実行しない。")


def main() -> None:
    banner(f"TOOL-03 deploy: migrate → {SCHEMA}")
    use_task_schema()
    connect_schema().close()  # 所有台帳ゲート(自分が作ったスキーマか)

    from jetuse_core.db import connect
    from jetuse_core.migrate import migrate

    applied = migrate()
    print("applied:", applied or "(none — up to date)")
    again = migrate()
    print("再適用:", again or "(none — 冪等)")
    if again:
        sys.exit(f"マイグレーションが冪等でない(2 回目で {again} を再適用した)。中止。")

    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT column_name, data_type, nullable FROM user_tab_columns "
            "WHERE table_name = 'HTTP_TOOLS' ORDER BY column_id"
        )
        cols = cur.fetchall()
        cur.execute("SELECT version FROM schema_migrations ORDER BY version")
        versions = [r[0] for r in cur.fetchall()]
    if not cols:
        sys.exit("HTTP_TOOLS 表が作られていない。中止。")
    print("\nHTTP_TOOLS 列:")
    for c in cols:
        print(f"  {c[0]:20s} {c[1]:12s} nullable={c[2]}")
    print("\n適用済みマイグレーション:", ", ".join(versions))

    params = next((c for c in cols if c[0] == "PARAMETERS"), None)
    if not params or params[1] != "CLOB":
        sys.exit(f"PARAMETERS 列が CLOB でない: {params}。入れ子 JSON を保持できない。中止。")
    print("\nPARAMETERS は CLOB = 入れ子スキーマをそのまま保持できる"
          "(TOOL-03 はマイグレーションを足していない)")


if __name__ == "__main__":
    main()
