"""方式①: OCI Vector Store + file_search（現行 jetuse_core/rag.py の経路）の実機検証。

検証したいこと（tasks/SPIKE-M1.md 完了条件）:
  - チャンク単位（またはファイル単位）の**属性を付与できるか**
  - 検索時に**メタデータフィルタ**を渡せるか
  - `file_search_call.results` と message annotations に**何が返るか**（実物のダンプ）

できない場合は「できなかった」で済ませず、**エラー本文そのもの**を証跡に残す。

実行: PYTHONPATH=spikes/spike_m1 .venv/bin/python spikes/spike_m1/method_a_vector_store.py
"""

import json
import os
import sys
import time

from common import (
    RESOURCE_TAG,
    assert_compartment,
    banner,
    client_args,
    is_ours,
    load_env,
    record_created,
)
from fixtures import QUERY, chunks

VS_NAME = "jetuse-spike-m1-vs"   # jetuse-spike- 接頭辞必須（teardown.py で片付ける）
MODEL = "openai.gpt-oss-120b"


PROJECT_NAME = "jetuse-spike-m1-project"
PROJECT_DESC = "SPIKE-M1 verification only. Delete after the spike."


def ensure_project(*, allow_create: bool = True) -> str:
    """検証用 GenAI プロジェクトを用意する（無ければ作り、台帳に記録する）。

    既存の同名プロジェクトは**台帳に載っているものだけ**再利用する。
    名前一致だけで他人のプロジェクトに相乗り（や削除）をしない。
    """
    import oci
    from jetuse_core.settings import get_settings

    comp = assert_compartment()   # 未承認コンパートメントへ作らない（fail-closed）
    s = get_settings()
    gai = oci.generative_ai.GenerativeAiClient(**client_args())
    preset = os.environ.get("PROJECT_OCID")
    if preset:
        # env で渡されても素通ししない。JetUse 本番用プロジェクトを指した .env で
        # そこへ Files を作ってしまう（＝既存リソースの変更）のを防ぐ。
        p = gai.get_generative_ai_project(preset).data
        if p.compartment_id != comp or p.display_name != PROJECT_NAME:
            raise SystemExit(f"PROJECT_OCID が指すプロジェクト（{p.display_name}）は"
                             "本スパイクのものではない。中止。")
        if not is_ours("genai_project", p.id):
            raise SystemExit("PROJECT_OCID が指すプロジェクトが台帳に無い。"
                             "自分が作ったものでなければ触らない。中止。")
        return preset
    for p in oci.pagination.list_call_get_all_results(
            gai.list_generative_ai_projects, s.compartment_ocid).data:
        if p.lifecycle_state != "ACTIVE" or p.display_name != PROJECT_NAME:
            continue
        if is_ours("genai_project", p.id):
            os.environ["PROJECT_OCID"] = p.id
            return p.id
        raise SystemExit(
            f"同名の GenAI プロジェクト {PROJECT_NAME} があるが台帳に無い。"
            " 他者のリソースの可能性があるため中止する（手動で確認すること）")
    if not allow_create:
        # 片付け経路（teardown）から呼ばれたとき、新しいリソースを作ってしまわないため。
        # dry-run が副作用でプロジェクトを増やすのは論外。
        raise SystemExit(f"GenAI プロジェクト {PROJECT_NAME} が無い（作成は許可されていない）")
    created = gai.create_generative_ai_project(
        oci.generative_ai.models.CreateGenerativeAiProjectDetails(
            compartment_id=s.compartment_ocid, display_name=PROJECT_NAME,
            description=PROJECT_DESC)).data
    record_created("genai_project", created.id, PROJECT_NAME)
    os.environ["PROJECT_OCID"] = created.id
    return created.id


def _clients(*, allow_create: bool = True):
    from jetuse_core.genai import make_cp_client, make_inference_client

    project = ensure_project(allow_create=allow_create)
    return make_cp_client(), make_inference_client(with_project=True, project_ocid=project)


def _dump(obj, limit: int = 20000) -> str:
    """SDK オブジェクトを JSON で丸ごと出す（何のフィールドが返るかを記録するため）。"""
    if hasattr(obj, "model_dump"):
        obj = obj.model_dump()
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)[:limit]


def _err(e: Exception, limit: int = 1800) -> None:
    print(f"  例外型: {type(e).__name__}")
    print("  --- エラー本文 ---")
    print("  " + str(e).replace("\n", "\n  ")[:limit])


def ensure_store(cp, dp) -> str:
    """Vector Store を用意し、**DP 側から見えるようになるまで待つ**。

    CP が completed でも DP への伝播に 10〜30 秒かかる（SPIKE-03 / rag.py の実機知見）。
    ここを待たずに files.create すると 404 が出て、属性の可否判定と混ざる。
    """
    banner("①-1 Vector Store の作成と DP 伝播待ち")
    vs_id = next((v.id for v in cp.vector_stores.list().data if v.name == VS_NAME), None)
    if vs_id:
        # 名前一致だけで既存ストアへファイルを足さない（他者のリソースを変更しうる）
        if not is_ours("vector_store", vs_id):
            raise SystemExit(
                f"同名の Vector Store {VS_NAME} があるが台帳に無い。"
                " 他者のリソースの可能性があるため中止する（手動で確認すること）")
        print(f"  既存を再利用（台帳に記録済み）: {vs_id}")
    else:
        vs = cp.vector_stores.create(name=VS_NAME, metadata={"purpose": RESOURCE_TAG})
        vs_id = vs.id
        record_created("vector_store", vs_id, VS_NAME)
        print(f"  created id={vs_id} status={vs.status}")
        for _ in range(30):
            st = cp.vector_stores.retrieve(vector_store_id=vs_id).status
            if st == "completed":
                print(f"  CP status={st}")
                break
            time.sleep(2)
    for attempt in range(30):
        try:
            dp.vector_stores.files.list(vector_store_id=vs_id)
            print(f"  DP から可視（{attempt + 1} 回目の試行）")
            return vs_id
        except Exception:  # noqa: BLE001 - 伝播待ちのリトライ
            time.sleep(5)
    raise RuntimeError(f"Vector Store {vs_id} が DP から見えない（伝播待ち枯渇）")


def upload_with_attributes(dp, vs_id: str) -> tuple[list[str], bool]:
    """ファイル登録時に attributes（任意メタデータ）を付けられるかを実際に試す。"""
    banner("①-2 ファイル登録時に attributes（任意メタデータ）を付けられるか")
    listed = dp.vector_stores.files.list(vector_store_id=vs_id).data
    existing = {f.id for f in listed}
    have = {(f.attributes or {}).get("chunk_id") for f in listed}
    want = {c["chunk_id"] for c in chunks()}
    if existing and have == want:
        print(f"  既に基準セット（{sorted(want)}）が登録済み。再アップロードしない")
        return sorted(existing), False
    if existing:
        # 件数だけ見て素通りすると、無関係なファイルが入ったストアで検証が成立してしまう
        raise SystemExit(f"ストアの中身が基準セットと違う（登録済み chunk_id={sorted(have)}）。"
                         " teardown してから作り直すこと")
    file_ids: list[str] = []
    ok_count = 0
    for i, c in enumerate(chunks()):
        name = (f"{c['chunk_id']}__v{c['version']}__"
                f"{'current' if c['current_version'] else 'stale'}__{c['kind']}.txt")
        f = dp.files.create(file=(name, c["text"].encode("utf-8")), purpose="assistants")
        file_ids.append(f.id)
        attrs = {
            "chunk_id": c["chunk_id"],
            "file": c["file"], "version": c["version"], "sheet": c["sheet"],
            "cells": c["cells"], "sha256": c["sha256"], "kind": c["kind"],
            "current_version": "Y" if c["current_version"] else "N",
        }
        if i == 0:
            print(f"  渡そうとした attributes: {json.dumps(attrs, ensure_ascii=False)}")
        try:
            dp.vector_stores.files.create(
                vector_store_id=vs_id, file_id=f.id, attributes=attrs)
            ok_count += 1
            if i == 0:
                print(f"  attributes 付き登録 OK: {name}")
        except Exception as e:  # noqa: BLE001 - 可否判定が目的。本文をそのまま記録する
            if i == 0:
                print(f"  attributes 付き登録 NG: {name}")
                _err(e)
            dp.vector_stores.files.create(vector_store_id=vs_id, file_id=f.id)
    # 1 件でも成功したら「対応」ではなく、**全件成功して初めて**対応とする
    attr_supported = ok_count == len(file_ids)
    verdict = "可" if attr_supported else f"部分的（{ok_count}/{len(file_ids)}）"
    print(f"\n  => attributes 付与: {verdict}／登録 {len(file_ids)} 件")
    if not attr_supported:
        raise SystemExit("attributes 付き登録が全件成功しなかった。①の属性検証は成立していない")
    return file_ids, attr_supported


def wait_indexed(dp, vs_id: str, file_ids: list[str]) -> None:
    """**今回登録した file_id だけ**の完了を待つ（ストア内の総数では偽陽性になる）。"""
    banner("①-3 取り込み完了待ち")
    want = set(file_ids)
    deadline = time.time() + 300
    while time.time() < deadline:
        states = {f.id: f.status for f in dp.vector_stores.files.list(vector_store_id=vs_id).data}
        done = [fid for fid in want if states.get(fid) == "completed"]
        # queued / in_progress は取り込み中。失敗扱いにしない（実機で queued を誤検知した）
        pending = (None, "completed", "in_progress", "queued")
        failed = {fid: states[fid] for fid in want if states.get(fid) not in pending}
        print(f"  completed {len(done)}/{len(want)}" + (f" failed={failed}" if failed else ""))
        if failed:
            raise RuntimeError(f"取り込みに失敗したファイルがある: {failed}")
        if len(done) == len(want):
            return
        time.sleep(10)
    raise RuntimeError("取り込みが完了しないままタイムアウト（この先の検索結果は信用できない）")


def show_stored_file(dp, vs_id: str, file_ids: list[str]) -> None:
    banner("①-4 登録済みファイルに何が保持されているか（1 件ダンプ）")
    vf = dp.vector_stores.files.retrieve(vector_store_id=vs_id, file_id=file_ids[0])
    print(_dump(vf))


def try_store_search(dp, vs_id: str) -> bool:
    """検索のみの口（POST /vector_stores/{id}/search）が使えるかを試す。"""
    banner("①-5 Vector Store 検索 API（生成なしの retrieval 単体）の可否")
    ok = True
    for label, kwargs in [
        ("フィルタ無し", {}),
        ("属性フィルタ付き",
         {"filters": {"type": "eq", "key": "current_version", "value": "Y"}}),
    ]:
        print(f"\n  [{label}] vector_stores.search(query=..., {json.dumps(kwargs)})")
        try:
            r = dp.vector_stores.search(vector_store_id=vs_id, query=QUERY, **kwargs)
            print("  " + _dump(r).replace("\n", "\n  "))
            if not r.data:
                print("  -> NG: 検索結果が空")
                ok = False
        except Exception as e:  # noqa: BLE001 - 可否判定が目的
            _err(e)
            ok = False
    return ok


def search(dp, vs_id: str, *, filters: dict | None) -> bool:
    label = "フィルタ有り" if filters else "フィルタ無し"
    banner(f"①-6 Responses + file_search（{label}）")
    tool: dict = {"type": "file_search", "vector_store_ids": [vs_id]}
    if filters:
        tool["filters"] = filters
    print(f"  tools={json.dumps([tool], ensure_ascii=False)}")
    try:
        resp = dp.responses.create(
            model=MODEL, input=QUERY, tools=[tool],
            include=["file_search_call.results"],
        )
    except Exception as e:  # noqa: BLE001 - 可否判定が目的。本文をそのまま記録する
        print("  -> NG")
        _err(e, 2500)
        return False
    hits: list[str] = []
    for item in resp.output or []:
        t = getattr(item, "type", "")
        if t == "file_search_call":
            hits = [(r.attributes or {}).get("current_version", "?")
                    for r in (getattr(item, "results", None) or [])]
            print("  --- file_search_call（results の全フィールド）---")
            print("  " + _dump(item).replace("\n", "\n  "))
        elif t == "message":
            for part in getattr(item, "content", None) or []:
                anns = getattr(part, "annotations", None) or []
                print(f"  --- message annotations（{len(anns)} 件）---")
                for a in anns:
                    print("  " + _dump(a).replace("\n", "\n  "))
    # API が 200 を返しただけでは成功にしない。結果があること、
    # フィルタ有りなら旧版が 1 件も混ざっていないことまで見る。
    if not hits:
        print("  -> NG: file_search の結果が空")
        return False
    if filters and {"N", "?"} & set(hits):
        print(f"  -> NG: 版フィルタ有りなのに旧版/属性欠損が混ざった: {hits}")
        return False
    print(f"  判定: ヒット {len(hits)} 件 / current_version={hits}")
    return True


def main() -> None:
    load_env()
    cp, dp = _clients()
    vs_id = ensure_store(cp, dp)
    file_ids, _ = upload_with_attributes(dp, vs_id)
    wait_indexed(dp, vs_id, file_ids)
    show_stored_file(dp, vs_id, file_ids)
    ok_store_search = try_store_search(dp, vs_id)
    ok_plain = search(dp, vs_id, filters=None)
    ok_filtered = search(dp, vs_id,
                         filters={"type": "eq", "key": "current_version", "value": "Y"})
    # 必須の呼び出しが落ちたまま exit 0 にしない（自動処理が完了と誤認する）
    if not (ok_store_search and ok_plain and ok_filtered):
        sys.exit("① の検索（search API / Responses+file_search）が成立していない")


if __name__ == "__main__":
    main()
