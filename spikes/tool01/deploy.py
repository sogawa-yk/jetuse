"""TOOL-01 の実環境デプロイ: run 固有スキーマへ全マイグレーションを適用する。

共有 loop ADB を再利用し、スキーマだけで隔離する(ADB は増やさない)。接続・所有台帳・
fail-closed ゲートは RAGM-02 の検証共通部(`spikes/ragm02/common.py`)をそのまま使う。

実行:
  env SPIKE_SCHEMA_PREFIX=JETUSE_TOOL01 SPIKE_HOME=/tmp/jetuse-tool01 \
      PYTHONPATH=spikes/ragm02:spikes/tool01:packages/api \
      .venv/bin/python spikes/tool01/deploy.py
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
    banner(f"TOOL-01 deploy: migrate → {SCHEMA}")
    use_task_schema()
    connect_schema().close()  # 所有台帳ゲート(自分が作ったスキーマか)

    from jetuse_core.db import connect
    from jetuse_core.migrate import migrate

    applied = migrate()
    print("applied:", applied or "(none — up to date)")

    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT column_name, data_type, nullable FROM user_tab_columns "
            "WHERE table_name = 'HTTP_TOOLS' ORDER BY column_id"
        )
        cols = cur.fetchall()
        cur.execute(
            "SELECT constraint_name, constraint_type FROM user_constraints "
            "WHERE table_name = 'HTTP_TOOLS' ORDER BY constraint_name"
        )
        cons = cur.fetchall()
    if not cols:
        sys.exit("HTTP_TOOLS 表が作られていない。中止。")
    print("\nHTTP_TOOLS 列:")
    for c in cols:
        print(f"  {c[0]:20s} {c[1]:12s} nullable={c[2]}")
    print("\nHTTP_TOOLS 制約:")
    for c in cons:
        print(f"  {c[0]:30s} {c[1]}")
    # 平文の秘密を置く列が無いことを構造で示す(列名に SECRET を含むのは OCID 参照のみ)
    secretish = [c[0] for c in cols if "SECRET" in c[0] and not c[0].endswith("_OCID")]
    if secretish:
        sys.exit(f"平文の秘密を置きうる列がある: {secretish}")
    print("\n平文の秘密列なし(auth_secret_ocid = Vault の参照のみ)")


if __name__ == "__main__":
    main()
