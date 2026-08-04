"""TOOL-01 の OCI 側検証資源を片付ける(既定は dry-run。実削除は `--yes`)。

消すのは**この run が作ったもの**だけ:
- Object Storage バケット `jetuse-spike-tool01-<スキーマ接尾辞>`(PAR とオブジェクトごと)。
  名前に run 固有のスキーマ接尾辞が入るので、他 run のものと衝突しない。

**Vault 秘密は消さない**。名前が固定で所有を証明できないため、別 run / 人が管理する同名の
秘密を消しうる(「既存リソースは参照のみ」)。不要になったら人が削除予約する。
ADB スキーマの片付けは `spikes/ragm02/teardown.py --yes`(所有台帳ゲートつき)で行う。

実行: env SPIKE_SCHEMA_PREFIX=JETUSE_TOOL01 SPIKE_HOME=/tmp/jetuse-tool01 \
        PYTHONPATH=spikes/ragm02:spikes/tool01:packages/api \
        .venv/bin/python spikes/tool01/teardown.py [--yes]
"""

import sys

from deploy import use_task_schema

from common import banner, require_schema
from e2e import SECRET_NAME, _os_client
from fixtures import PREFIX

SCHEMA = require_schema()


def main() -> None:
    apply = "--yes" in sys.argv
    use_task_schema()
    banner(f"TOOL-01 teardown（{'実削除' if apply else 'dry-run'}）")

    bucket = f"{PREFIX}-{SCHEMA.split('_')[-1].lower()}"
    client = _os_client()
    ns = client.get_namespace().data
    import oci

    try:
        pars = client.list_preauthenticated_requests(ns, bucket).data
        objs = client.list_objects(ns, bucket).data.objects
    except oci.exceptions.ServiceError as e:
        if e.status != 404:
            raise
        print(f"  バケット {bucket} は無い(片付け済み)")
        pars, objs = [], []
        bucket = ""

    for p in pars:
        print(f"  {'delete' if apply else 'would delete'} PAR {p.name}")
        if apply:
            client.delete_preauthenticated_request(ns, bucket, p.id)
    for o in objs:
        print(f"  {'delete' if apply else 'would delete'} object {bucket}/{o.name}")
        if apply:
            client.delete_object(ns, bucket, o.name)
    if bucket and apply:
        client.delete_bucket(ns, bucket)
        print(f"  deleted bucket {bucket}")

    # Vault の秘密には**触らない**。名前が固定(`jetuse-spike-tool01-apikey`)で、この run が
    # 作ったことを示す所有台帳が無い以上、別 run / 人が管理する同名の秘密を消しうる
    # (「既存リソースは参照のみ」に反する — レビュー TOOL-01-004)。片付けは人が行う。
    print(f"\nVault 秘密 {SECRET_NAME} は自動削除しない(所有を証明できないため)。")
    print("  不要なら人が実行する:")
    print(f"    oci vault secret schedule-deletion --secret-id <{SECRET_NAME} の OCID> \\")
    print("      --time-of-deletion <7日以上先の時刻>")

    if not apply:
        print("\n(dry-run。実削除は --yes)")


if __name__ == "__main__":
    main()
