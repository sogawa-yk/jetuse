"""RAGM-01: Vector Store 属性とフィルタの検証(jetuse_core/rag_metadata.py)。

SPIKE-M1 ①-b で「存在しないキーで絞るとエラーにならず 0 件」を実測したため、
キー名の担保はアプリ側の責任。ここが唯一の門番なので否定側を厚く固定する。
"""

import pytest

from jetuse_core import rag_metadata as rm

# --- attributes(取り込み時) ---


def test_normalize_attributes_keeps_values_and_drops_empty_keys():
    out = rm.normalize_attributes({
        "file": "在庫連携API仕様書.xlsx",
        "version": "2.0",
        "sheet": "API一覧",
        "cells": "B12:F12",
        "current_version": "Y",
        "kind": "",          # 空文字はキーごと省く(フィルタが静かに効かなくなるため)
        "sha256": None,      # None も同様
        "chunk_id": "   ",   # 空白のみも「値が無い」扱い
    })
    assert out == {
        "file": "在庫連携API仕様書.xlsx", "version": "2.0", "sheet": "API一覧",
        "cells": "B12:F12", "current_version": "Y",
    }


def test_normalize_attributes_keeps_falsy_numbers_and_booleans():
    """0 / False は「値が無い」ではないので残す(truthy 判定で落とさない)。"""
    out = rm.normalize_attributes({"chunk_id": 0, "current_version": False})
    assert out == {"chunk_id": 0, "current_version": False}


def test_normalize_attributes_rejects_unknown_key():
    with pytest.raises(rm.MetadataError) as e:
        rm.normalize_attributes({"versoin": "2.0"})
    assert "versoin" in str(e.value)


def test_normalize_attributes_rejects_oversized_value():
    """①-d: 値 512 文字超は OCI が拒否する。切り詰めると sha256/version が
    別物になりフィルタが静かに外れるため、切り詰めずに拒否する。"""
    ok = rm.normalize_attributes({"file": "a" * rm.MAX_ATTRIBUTE_VALUE_CHARS})
    assert len(ok["file"]) == rm.MAX_ATTRIBUTE_VALUE_CHARS
    with pytest.raises(rm.MetadataError):
        rm.normalize_attributes({"file": "a" * (rm.MAX_ATTRIBUTE_VALUE_CHARS + 1)})


def test_normalize_attributes_rejects_nested_and_non_scalar():
    """①-d: 入れ子は `must be a string, number, or boolean` で拒否される。"""
    for bad in ({"sheet": {"name": "x"}}, {"sheet": ["a"]}, {"sheet": object()}):
        with pytest.raises(rm.MetadataError):
            rm.normalize_attributes(bad)


def test_normalize_attributes_rejects_non_finite_number():
    for bad in (float("nan"), float("inf")):
        with pytest.raises(rm.MetadataError):
            rm.normalize_attributes({"chunk_id": bad})


def test_kind_is_string_only_on_both_ingest_and_search():
    """RAGM-04: `kind` は文字列のみ。数値・真偽は**黙って文字列化せず**拒否する。

    文字列化すると、マネージド側は `0`(数値)・ADB 側は `"0"`(文字列)で入り、
    同じフィルタがバックエンドで違う結果になる(ADB の build_where は非文字列を拒否する)。
    """
    for bad in (0, 1, 1.5, True, False):
        with pytest.raises(rm.MetadataError) as e:
            rm.normalize_attributes({"kind": bad})
        assert "string" in str(e.value)
        with pytest.raises(rm.MetadataError):
            rm.validate_filters({"type": "eq", "key": "kind", "value": bad})
    assert rm.normalize_attributes({"kind": "spec"}) == {"kind": "spec"}
    assert rm.validate_filters({"type": "eq", "key": "kind", "value": "spec"}) == {
        "type": "eq", "key": "kind", "value": "spec",
    }


def test_kind_length_limit_is_in_bytes_and_applies_to_filters_too():
    """上限は ADB 列(VARCHAR2(32))に合わせたまま**広げない**。

    検索側にも同じ上限を掛ける: 取り込めない長さの値で絞っても必ず 0 件になり、
    ①-b と同じ「静かに該当なし」になるため。
    """
    assert len("分類" * 6) < rm.MAX_KIND_BYTES < len(("分類" * 6).encode("utf-8"))
    for bad in ("k" * (rm.MAX_KIND_BYTES + 1), "分類" * 6):
        with pytest.raises(rm.MetadataError):
            rm.normalize_attributes({"kind": bad})
        with pytest.raises(rm.MetadataError):
            rm.validate_filters({"type": "eq", "key": "kind", "value": bad})
    assert rm.normalize_attributes({"kind": "k" * rm.MAX_KIND_BYTES})["kind"]


def test_normalize_attributes_rejects_non_mapping():
    for bad in ("{}", [("file", "a")], 3):
        with pytest.raises(rm.MetadataError):
            rm.normalize_attributes(bad)


def test_normalize_attributes_empty_input_is_empty_dict():
    assert rm.normalize_attributes(None) == {}
    assert rm.normalize_attributes({}) == {}


def test_allowed_keys_fit_provider_limit():
    """①-d: キーは 16 個まで。許可キー集合自体がそれを超えないことを固定する。"""
    assert len(rm.ATTRIBUTE_KEYS) <= rm.MAX_ATTRIBUTE_KEYS


# --- filters(検索時) ---


def test_validate_filters_accepts_eq_and_normalizes():
    f = rm.validate_filters({"type": "eq", "key": "current_version", "value": "Y"})
    assert f == {"type": "eq", "key": "current_version", "value": "Y"}


def test_validate_filters_drops_extraneous_fields():
    """余計なフィールドは素通しせず落とす(上流に未知の指示を渡さない)。"""
    f = rm.validate_filters(
        {"type": "eq", "key": "version", "value": "2.0", "boost": 3}
    )
    assert f == {"type": "eq", "key": "version", "value": "2.0"}


def test_validate_filters_accepts_compound():
    f = rm.validate_filters({
        "type": "and",
        "filters": [
            {"type": "eq", "key": "current_version", "value": "Y"},
            {"type": "gte", "key": "version", "value": "2.0"},
        ],
    })
    assert f["type"] == "and" and len(f["filters"]) == 2


def test_validate_filters_rejects_unknown_key_at_any_depth():
    """タイポは 0 件になるだけでエラーにならない(①-b)。深い位置でも弾く。"""
    with pytest.raises(rm.MetadataError) as e:
        rm.validate_filters({
            "type": "or",
            "filters": [
                {"type": "eq", "key": "current_version", "value": "Y"},
                {"type": "eq", "key": "verison", "value": "2.0"},
            ],
        })
    assert "verison" in str(e.value)


def test_validate_filters_rejects_in_operator():
    """①-b: `in` は上流が 400(values 配列を解釈せず value を要求する)。"""
    with pytest.raises(rm.MetadataError) as e:
        rm.validate_filters(
            {"type": "in", "key": "version", "values": ["1.0", "2.0"]}
        )
    assert "in" in str(e.value)


def test_validate_filters_rejects_unknown_type_and_malformed():
    bad_cases = [
        {"type": "matches", "key": "file", "value": "a"},   # 未知の演算子
        {"key": "file", "value": "a"},                       # type 欠落
        {"type": "eq", "value": "a"},                        # key 欠落
        {"type": "eq", "key": "file"},                       # value 欠落
        {"type": "and", "filters": []},                      # 空の複合
        {"type": "and", "filters": {"type": "eq"}},          # filters が配列でない
        {"type": "eq", "key": "file", "value": {"x": 1}},    # 値が非スカラー
        "current_version=Y",                                  # そもそも dict でない
    ]
    for bad in bad_cases:
        with pytest.raises(rm.MetadataError):
            rm.validate_filters(bad)


def test_validate_filters_rejects_too_deep_nesting():
    f = {"type": "eq", "key": "file", "value": "a"}
    for _ in range(rm.MAX_FILTER_DEPTH + 1):
        f = {"type": "and", "filters": [f]}
    with pytest.raises(rm.MetadataError):
        rm.validate_filters(f)


def test_validate_filters_none_is_none():
    assert rm.validate_filters(None) is None


def test_validate_filters_rejects_null_child():
    """複合フィルタの子の null は素通りさせない(最上位の None だけが「絞り込み無し」)。"""
    with pytest.raises(rm.MetadataError):
        rm.validate_filters({"type": "and", "filters": [
            {"type": "eq", "key": "version", "value": "2.0"}, None,
        ]})


def test_normalize_attributes_accepts_huge_int():
    """Python の int は任意精度。math.isfinite に渡して OverflowError(=500)にしない。"""
    big = 10 ** 400
    assert rm.normalize_attributes({"chunk_id": big}) == {"chunk_id": big}
