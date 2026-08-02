"""TOOL-02 の実環境デプロイ: run 固有スキーマへ全マイグレーションを適用する。

共有 loop ADB を再利用し、スキーマだけで隔離する(ADB は増やさない)。接続・所有台帳・
fail-closed ゲートは RAGM-02 の検証共通部(`spikes/ragm02/common.py`)をそのまま使う。
021 で追加した 2 列が **NULL 可**で入ったこと(=既存行の挙動が変わらないこと)を構造で示す。

実行:
  env SPIKE_SCHEMA_PREFIX=JETUSE_TOOL02 SPIKE_HOME=/tmp/jetuse-tool02 \
      PYTHONPATH=spikes/ragm02:spikes/tool02:packages/api \
      .venv/bin/python spikes/tool02/deploy.py
"""

import os
import sys

from common import banner, connect_schema, prepare_env, require_schema, secret

SCHEMA = require_schema()
NEW_COLUMNS = {"EXTRA_HEADERS", "IDEMPOTENCY_HEADER"}


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
    banner(f"TOOL-02 deploy: migrate → {SCHEMA}")
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

    added = {c[0]: c for c in cols if c[0] in NEW_COLUMNS}
    missing = NEW_COLUMNS - added.keys()
    if missing:
        sys.exit(f"021 の列が無い: {sorted(missing)}。中止。")
    not_null = [n for n, c in added.items() if c[2] != "Y"]
    if not_null:
        sys.exit(f"021 の列が NOT NULL になっている: {not_null}(既存行が壊れる)。中止。")
    print("\n021 の 2 列は NULL 可 = 既存ツールは両方 NULL のまま動く")
    secretish = [c[0] for c in cols if "SECRET" in c[0] and not c[0].endswith("_OCID")]
    if secretish:
        sys.exit(f"秘密の実値を置く列がある: {secretish}")
    # ここで言えるのは「**秘密専用の**平文列が無い」ことだけ。TOOL-02 で足した
    # EXTRA_HEADERS は任意の平文値を保存できる = そこに秘密を書かれれば平文で載る
    # (機構では強制できない残存リスク。ADR-0023 §4 の人間判断)
    print("秘密専用の平文列なし(認証は auth_secret_ocid = Vault の参照のみ)")
    print("ただし EXTRA_HEADERS は任意の平文値を保存できる。"
          "秘密を入れない運用契約が前提(ADR-0023 §4・人間レビュー中)")


if __name__ == "__main__":
    main()
