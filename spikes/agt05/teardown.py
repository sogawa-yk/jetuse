"""AGT-05 の OCI 側検証資源を片付ける（既定は dry-run。実削除は `--yes`）。

消すのは **この run が作ったもの**だけ:
- アップロードした RAG ファイル（Vector Store 内のファイル・Files API・Object Storage 原本・
  ADB のチャンク）= `rag.delete_file` と同じ経路
- 接頭辞 `jetuse-spike-agt05-` で始まる Vector Store のうち、**この run のスキーマ名の
  接尾辞を持つもの**（他タスク・他 run の箱は名前が一致しないので触らない）

登録した外部 HTTP ツールは ADB スキーマの中の行なので、スキーマごと消える
（`spikes/ragm02/teardown.py --yes`。所有台帳ゲートつき）。

実行: PYTHONPATH=spikes/ragm02:spikes/agt05:packages/api .venv/bin/python \
        spikes/agt05/teardown.py [--yes]
"""

import sys

from common import banner, connect_schema, require_schema
from e2e import OWNER, mask, use_task_schema
from fixtures import PREFIX

SCHEMA = require_schema()


def main() -> None:
    apply = "--yes" in sys.argv
    use_task_schema()
    connect_schema().close()  # 台帳ゲート（自分が作ったスキーマか）を通す

    from jetuse_core import rag
    from jetuse_core.genai import make_cp_client

    expected_name = f"{PREFIX}-{SCHEMA.rsplit('_', 1)[-1].lower()}"
    files = rag.list_files(OWNER)
    store_id = rag.get_store_id(OWNER)

    # **何かを消す前に**所有を確かめる（fail-closed）。ファイル削除は Vector Store 内の
    # ファイル・Files API・Object Storage 原本まで消すので、箱の身元が確認できない状態で
    # 1 件でも消すと、他タスクの資源を壊してから止まることになる。
    cp = make_cp_client()
    if store_id:
        actual = cp.vector_stores.retrieve(vector_store_id=store_id).name
        if actual != expected_name:
            sys.exit(f"Vector Store の名前が一致しない（{actual} != {expected_name}）。"
                     "何も削除せずに中止する。")
    elif files:
        sys.exit("登録簿に Vector Store が無いのにファイルが残っている。"
                 "身元を確かめられないので何も削除せずに中止する。")
    foreign = [f["filename"] for f in files if not f["filename"].startswith(PREFIX)]
    if foreign:
        sys.exit(f"接頭辞 {PREFIX} を持たないファイルが混ざっている: {foreign[:3]}。"
                 "何も削除せずに中止する。")

    banner("削除対象")
    for f in files:
        print(f"  file {f['id']} {f['filename']} ({mask(f['oci_file_id'])})")
    print(f"  vector store {mask(store_id)}（名前: {expected_name} — 照合済み）")
    if not apply:
        print("\ndry-run。実削除は --yes を付けて実行する")
        return

    for f in files:
        print(f"  削除: {f['id']} -> {rag.delete_file(OWNER, f['id'])}")
    if store_id:
        cp.vector_stores.delete(vector_store_id=store_id)
        print(f"  Vector Store 削除: {expected_name}")
    print("\ndone（ADB スキーマは spikes/ragm02/teardown.py --yes で片付ける）")


if __name__ == "__main__":
    main()
