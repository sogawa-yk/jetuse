"""方式①の**限界**を測る（属性・フィルタが使えることは method_a_vector_store.py で確認済み）。

ここで測るのは「どこまでできるか」:
  (a) 属性は**ファイル単位かチャンク単位か** — 複数チャンクに割れる 1 ファイルを入れて、
      各チャンクが別々の cells/sheet を持てるかを見る（出典粒度の上限）
  (b) フィルタの**表現力** — eq / in / and / or / gte が通るか
  (c) 取り込み後に属性を**更新**できるか（版が上がったときの current_version 付け替え）
  (d) 属性の**個数・値長**の上限

実行: PYTHONPATH=spikes/spike_m1 .venv/bin/python spikes/spike_m1/method_a_limits.py
"""

import json
import time

from common import banner, load_env, require_owned_store
from method_a_vector_store import VS_NAME, _clients, _dump, _err

# 3 チャンク以上に割れる長さの架空本文（1 ファイル = 複数チャンク）
MULTI_NAME = "multi__仕様書全体.txt"
MULTI_TEXT = "\n\n".join(
    f"第{i}章 サンプル在庫連携APIの規定その{i}。" + ("この章は架空の規定文である。" * 60)
    for i in range(1, 6)
)


def find_store(cp):
    vs_id = next((v.id for v in cp.vector_stores.list().data if v.name == VS_NAME), None)
    if not vs_id:
        raise SystemExit(f"{VS_NAME} が無い。先に method_a_vector_store.py を実行すること")
    # 単独実行でも台帳ゲートを通す（同名の他人のストアを変更しないため）
    require_owned_store(vs_id, VS_NAME)
    return vs_id


def multi_chunk_granularity(dp, vs_id: str) -> str:
    banner("①-L1 属性はファイル単位かチャンク単位か（複数チャンクに割れる 1 ファイル）")
    f = dp.files.create(file=(MULTI_NAME, MULTI_TEXT.encode("utf-8")), purpose="assistants")
    attrs = {"file": "サンプル在庫連携API仕様書.xlsx", "version": "2.0",
             "sheet": "全体", "cells": "A1:Z999", "kind": "spec", "current_version": "Y"}
    print(f"  1 ファイル（{len(MULTI_TEXT)} 文字）を登録\n"
          f"  attributes={json.dumps(attrs, ensure_ascii=False)}")
    dp.vector_stores.files.create(vector_store_id=vs_id, file_id=f.id, attributes=attrs)
    for _ in range(30):
        st = dp.vector_stores.files.retrieve(vector_store_id=vs_id, file_id=f.id).status
        if st == "completed":
            break
        time.sleep(10)
    print(f"  取り込み status={st}")
    if st != "completed":
        # 取り込めていない状態でヒット 0 件を「チャンク粒度なし」と誤読しない
        raise SystemExit(f"検証用ファイルの取り込みが完了しなかった（status={st}）。判定不能")
    r = dp.vector_stores.search(vector_store_id=vs_id, query="サンプル在庫連携APIの規定",
                                max_num_results=10)
    hits = [d for d in r.data if d.file_id == f.id]
    print(f"  このファイル由来のヒット（チャンク）数: {len(hits)}")
    for h in hits:
        chunk_id = (h.model_dump().get("additional_properties") or {}).get("chunk_id")
        print(f"    chunk_id={chunk_id} cells={h.attributes.get('cells')} "
              f"sheet={h.attributes.get('sheet')} text={h.content[0].text[:28]}…")
    if not hits:
        raise SystemExit("複数チャンクファイルのヒットが 0 件。粒度の判定ができない")
    distinct = {json.dumps(h.attributes, ensure_ascii=False, sort_keys=True) for h in hits}
    print(f"  => 異なる attributes の種類: {len(distinct)} "
          "（1 なら属性はファイル単位＝チャンクごとの cells は持てない）")
    if len(distinct) != 1:
        raise SystemExit(f"属性の種類が {len(distinct)} 種。ファイル単位という結論が成立しない")
    return f.id


def filter_expressiveness(dp, vs_id: str) -> None:
    banner("①-L2 フィルタの表現力（eq / in / and / or / gte）")
    cases = {
        "eq": {"type": "eq", "key": "kind", "value": "constraint"},
        "in": {"type": "in", "key": "version", "values": ["2.0"]},
        "and": {"type": "and", "filters": [
            {"type": "eq", "key": "current_version", "value": "Y"},
            {"type": "eq", "key": "kind", "value": "constraint"}]},
        "or": {"type": "or", "filters": [
            {"type": "eq", "key": "sheet", "value": "制約"},
            {"type": "eq", "key": "sheet", "value": "API一覧"}]},
        "gte(文字列比較)": {"type": "gte", "key": "version", "value": "2.0"},
        "存在しないキー": {"type": "eq", "key": "not_exists", "value": "x"},
    }
    for label, filt in cases.items():
        print(f"\n  [{label}] filters={json.dumps(filt, ensure_ascii=False)}")
        try:
            r = dp.vector_stores.search(
                vector_store_id=vs_id, query="レート制限", filters=filt, max_num_results=10)
            names = [d.filename for d in r.data]
            print(f"    -> OK {len(names)} 件: {names}")
        except Exception as e:  # noqa: BLE001 - 可否判定が目的
            _err(e, 700)


def update_attributes(dp, vs_id: str) -> None:
    banner("①-L3 取り込み後に属性を更新できるか（版の付け替え）")
    target = next(d for d in dp.vector_stores.files.list(vector_store_id=vs_id).data
                  if (d.attributes or {}).get("current_version") == "Y")
    print(f"  対象 file_id={target.id} 現在 current_version="
          f"{(target.attributes or {}).get('current_version')}")
    try:
        updated = dp.vector_stores.files.update(
            vector_store_id=vs_id, file_id=target.id,
            attributes={**(target.attributes or {}), "current_version": "N"})
        print("  -> 更新 OK:")
        print("  " + _dump(updated).replace("\n", "\n  "))
        dp.vector_stores.files.update(
            vector_store_id=vs_id, file_id=target.id,
            attributes={**(target.attributes or {}), "current_version": "Y"})
        print("  （元に戻した）")
    except Exception as e:  # noqa: BLE001 - 可否判定が目的
        _err(e, 900)


def attribute_limits(dp, vs_id: str) -> None:
    banner("①-L4 属性の個数・値長の上限")
    for label, attrs in [
        ("キー 20 個", {f"k{i}": "v" for i in range(20)}),
        ("値 600 文字", {"long": "あ" * 600}),
        ("値が数値型", {"num": 123}),
        ("値が入れ子オブジェクト", {"nested": {"a": 1}}),
    ]:
        f = dp.files.create(file=(f"limit_{label}.txt", b"limit probe"), purpose="assistants")
        print(f"\n  [{label}]")
        try:
            dp.vector_stores.files.create(
                vector_store_id=vs_id, file_id=f.id, attributes=attrs)
            print("    -> 受理された")
        except Exception as e:  # noqa: BLE001 - 可否判定が目的
            _err(e, 700)
        finally:
            try:
                dp.files.delete(f.id)
            except Exception:  # noqa: BLE001 - best-effort
                pass


def cleanup(dp, vs_id: str, file_id: str) -> None:
    """この検証で足したファイルを消し、ストアを 10 件の基準セットに戻す。

    残すと後続のレイテンシ計測が「10 チャンク条件」でなくなる（実際に一度ずれた）。
    """
    banner("①-L5 検証で追加したファイルの後始末")
    for step, fn in (("vector store から削除",
                      lambda: dp.vector_stores.files.delete(vector_store_id=vs_id,
                                                            file_id=file_id)),
                     ("files から削除", lambda: dp.files.delete(file_id))):
        try:
            fn()
            print(f"  ok: {step}")
        except Exception as e:  # noqa: BLE001 - 結果は必ず出し、最後に件数で判定する
            print(f"  NG: {step} ({type(e).__name__})")
    remaining = len(dp.vector_stores.files.list(vector_store_id=vs_id).data)
    print(f"  ストア内のファイル数: {remaining}（基準セットは 10）")
    if remaining != 10:
        raise SystemExit(f"後始末に失敗しストアが {remaining} 件のまま。基準セットへ戻せていない")


def main() -> None:
    load_env()
    cp, dp = _clients()
    vs_id = find_store(cp)
    multi_id = None
    try:
        multi_id = multi_chunk_granularity(dp, vs_id)
        filter_expressiveness(dp, vs_id)
        update_attributes(dp, vs_id)
        attribute_limits(dp, vs_id)
    finally:
        # どこで落ちても検証用ファイルは残さない（後続のレイテンシ計測を汚す）
        if multi_id:
            cleanup(dp, vs_id, multi_id)


if __name__ == "__main__":
    main()
