"""TOOL-02 の OCI 側検証資源を片付ける(既定は dry-run。実削除は `--yes`)。

消すのは**この run が作ったもの**だけ: Vault 秘密 `jetuse-spike-tool02-apikey` のうち、
freeform タグ `jetuse_spike_run` がこの run のスキーマ名と一致するものに限る。
名前だけで消すと、別 run / 人が管理する同名の秘密を消しうる(TOOL-01 の review-4 で
「所有を証明できないなら消さない」とした指摘。ここでは**印を付けて証明する**ことで解いた)。

秘密の削除は OCI では**削除予約**(最短 1 日先)であり、即時削除はできない。
ADB スキーマの片付けは `spikes/ragm02/teardown.py --yes`(所有台帳ゲートつき)。

実行: env SPIKE_SCHEMA_PREFIX=JETUSE_TOOL02 SPIKE_HOME=/tmp/jetuse-tool02 \
        PYTHONPATH=spikes/ragm02:spikes/tool02:packages/api \
        .venv/bin/python spikes/tool02/teardown.py [--yes]
"""

import datetime
import os
import sys

from deploy import use_task_schema

from common import banner, require_schema
from e2e import _vault_args
from fixtures import SECRET_NAME

SCHEMA = require_schema()


def main() -> None:
    apply = "--yes" in sys.argv
    use_task_schema()
    banner(f"TOOL-02 teardown（{'実削除' if apply else 'dry-run'}）")

    import oci

    vaults = oci.vault.VaultsClient(**_vault_args())
    comp = os.environ["ADB_COMPARTMENT_OCID"]
    targets = [
        s for s in vaults.list_secrets(comp, name=SECRET_NAME).data
        if s.lifecycle_state == "ACTIVE"
    ]
    if not targets:
        print(f"  Vault 秘密 {SECRET_NAME} は無い(片付け済み)")
    for s in targets:
        mine = (s.freeform_tags or {}).get("jetuse_spike_run") == SCHEMA
        if not mine:
            print(f"  skip {SECRET_NAME}: この run の印({SCHEMA})が無い。触らない。")
            continue
        when = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=2)
        print(f"  {'schedule-deletion' if apply else 'would schedule-deletion'} "
              f"{SECRET_NAME} → {when:%Y-%m-%d %H:%M}Z")
        if apply:
            vaults.schedule_secret_deletion(
                s.id, oci.vault.models.ScheduleSecretDeletionDetails(
                    time_of_deletion=when))

    print(f"\nADB スキーマ {SCHEMA} の片付けは spikes/ragm02/teardown.py --yes")
    if not apply:
        print("(dry-run。実削除は --yes)")


if __name__ == "__main__":
    main()
