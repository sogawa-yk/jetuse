"""RAGM-01: マネージド Vector Store のメタデータ属性とフィルタの検証。

取り込み(`rag.add_file`)と検索(`chat` の `tools[].filters`)の両方が通る唯一の門番。
SPIKE-M1(`docs/verification/SPIKE-M1.md` ①-b/①-d)の実測に基づく:

- 存在しないキーで絞っても **エラーにならず 0 件**になる。タイポが「該当なし」に化けて
  静かに全件を除外するため、許可キーはアプリ側で定数化し未知キーは弾く(ルート側で 422)。
- 属性の上限は キー 16 個 / 値 512 文字 / 文字列・数値・真偽のみ・入れ子不可。
- `in` フィルタは上流が受け付けない(`values` 配列を解釈せず `value` を要求する)。

**上限超過は切り詰めずに拒否する**(ADR-0020 §1 の実装判断)。sha256 や version を
黙って切り詰めると値が別物になり、①-b と同じ「静かに 0 件」を作ってしまうため。
"""

import math
from collections.abc import Mapping
from typing import Any

# ADR-0020 §1 が定める属性キー。ここに無いキーは取り込み・検索とも受け付けない。
ATTRIBUTE_KEYS: tuple[str, ...] = (
    "file", "version", "sheet", "cells", "sha256", "kind", "current_version", "chunk_id",
)

MAX_ATTRIBUTE_KEYS = 16          # ①-d: Metadata must not contain more than 16 key-value pairs
MAX_ATTRIBUTE_VALUE_CHARS = 512  # ①-d: exceeds max length of 512 characters
# `kind` だけは **ADB 側の列幅**(`rag_adb_chunks.kind VARCHAR2(32)`)に合わせて短く保つ。
# 同じ値を両バックエンドへ入れるので(PREP-01)、片方だけ入る長さを受け付けると
# 「マネージドでは絞れるのに ADB では取り込み自体が失敗する」というズレが起きる。
# **バイト長で見る**: 列は BYTE セマンティクスのことがあり、日本語 11 文字(33 バイト)は
# 文字数では通っても ORA-12899 になる(`rag._fit` が同じ理由でバイト長を使っている)。
MAX_KIND_BYTES = 32
MAX_FILTER_DEPTH = 5             # 複合フィルタの入れ子上限(病的な入力の打ち切り)

_COMPARISON_TYPES = frozenset({"eq", "ne", "gt", "gte", "lt", "lte"})
_COMPOUND_TYPES = frozenset({"and", "or"})
_UNSUPPORTED_TYPES = frozenset({"in", "nin"})

_KEYS_HINT = ", ".join(ATTRIBUTE_KEYS)


class MetadataError(ValueError):
    """属性・フィルタが受け付けられない。ルート側で 422 に正規化する。"""


def _check_scalar(value: Any, where: str) -> str | int | float | bool:
    """OCI が受け付けるスカラー(文字列/数値/真偽)であることを確認する。"""
    if isinstance(value, bool):  # bool は int のサブクラスなので先に判定する
        return value
    if isinstance(value, str):
        if len(value) > MAX_ATTRIBUTE_VALUE_CHARS:
            raise MetadataError(
                f"{where}: value exceeds max length of {MAX_ATTRIBUTE_VALUE_CHARS} characters"
            )
        return value
    if isinstance(value, int):
        # Python の int は任意精度。math.isfinite に渡すと OverflowError(=500)になるため
        # int はそのまま通し、有限性の判定は float だけに適用する
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise MetadataError(f"{where}: value must be a finite number")
        return value
    raise MetadataError(
        f"{where}: value must be a string, number, or boolean (nesting is not supported)"
    )


def _check_kind(value: Any, where: str) -> str:
    """`kind` は**文字列のみ**(RAGM-04)。取り込み・検索の両方でこれを通す。

    数値・真偽を受けると、マネージド側は値のまま(`0`)・ADB 側は列が
    VARCHAR2 なので文字列(`"0"`)で入り、**同じ条件が選んだバックエンドで違う結果**になる
    (`rag_adb.build_where` は非文字列のフィルタ値を拒否する)。黙って文字列化はしない —
    入れた値と検索条件が食い違うだけで、ズレが見えなくなる。
    長さ上限は ADB 列に合わせたまま広げない(狭い側で揃える)。検索側にも同じ上限を掛けるのは、
    取り込めない長さで絞っても必ず 0 件になり ①-b と同じ「静かに該当なし」になるため。
    """
    if not isinstance(value, str):
        raise MetadataError(
            f"{where}: value must be a string "
            "(numbers and booleans are not accepted: the adb backend stores kind in a "
            "VARCHAR2 column, so the same value would filter differently per backend)"
        )
    if len(value.encode("utf-8")) > MAX_KIND_BYTES:
        raise MetadataError(
            f"{where}: value must be at most {MAX_KIND_BYTES} bytes in UTF-8 "
            "(the adb backend stores it in a VARCHAR2(32) column)"
        )
    return value


def normalize_attributes(raw: Any) -> dict[str, str | int | float | bool]:
    """取り込み時の attributes を検証して返す。

    - 値が無いもの(None / 空文字 / 空白のみ)は **キーごと省く**。空文字を入れると
      フィルタが一致しなくなり、①-b と同じ「静かに 0 件」を作る。
    - `0` / `False` は「値が無い」ではないので残す。
    - 未知キー・上限超過・非スカラーは `MetadataError`(呼び出し側で 422)。
    """
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise MetadataError("attributes must be an object")
    out: dict[str, str | int | float | bool] = {}
    for key, value in raw.items():
        if key not in ATTRIBUTE_KEYS:
            raise MetadataError(f"unknown attribute key '{key}'. allowed: {_KEYS_HINT}")
        if value is None or (isinstance(value, str) and not value.strip()):
            continue  # 値が無いメタはキーごと省く
        where = f"attribute '{key}'"
        out[key] = _check_kind(value, where) if key == "kind" else _check_scalar(value, where)
    if len(out) > MAX_ATTRIBUTE_KEYS:
        raise MetadataError(
            f"attributes must not contain more than {MAX_ATTRIBUTE_KEYS} key-value pairs"
        )
    return out


def validate_filters(raw: Any, _depth: int = 0) -> dict | None:
    """検索フィルタ(`tools[].filters`)を検証し、既知フィールドだけの dict に正規化する。

    未知キーの静かな 0 件(①-b)を防ぐのが主目的。余計なフィールドは上流へ渡さない。
    """
    if raw is None:
        # 「フィルタ無し」を意味してよいのは最上位だけ。複合フィルタの子の null は
        # 検証を素通りして上流へ送られてしまう(レビュー F-003)ので弾く
        if _depth:
            raise MetadataError("filter must be an object")
        return None
    if _depth > MAX_FILTER_DEPTH:
        raise MetadataError(f"filters nested deeper than {MAX_FILTER_DEPTH}")
    if not isinstance(raw, Mapping):
        raise MetadataError("filter must be an object")
    ftype = raw.get("type")
    if not isinstance(ftype, str):
        raise MetadataError("filter requires a 'type'")
    if ftype in _UNSUPPORTED_TYPES:
        raise MetadataError(
            f"filter type '{ftype}' is not supported by the vector_store backend "
            "(SPIKE-M1 ①-b). use 'or' with 'eq' instead"
        )
    if ftype in _COMPOUND_TYPES:
        subs = raw.get("filters")
        if not isinstance(subs, list) or not subs:
            raise MetadataError(f"filter type '{ftype}' requires a non-empty 'filters' array")
        return {
            "type": ftype,
            "filters": [validate_filters(s, _depth + 1) for s in subs],
        }
    if ftype not in _COMPARISON_TYPES:
        raise MetadataError(
            f"unknown filter type '{ftype}'. allowed: "
            f"{', '.join(sorted(_COMPARISON_TYPES | _COMPOUND_TYPES))}"
        )
    key = raw.get("key")
    if key not in ATTRIBUTE_KEYS:
        raise MetadataError(f"unknown filter key '{key}'. allowed: {_KEYS_HINT}")
    if "value" not in raw:
        raise MetadataError(f"filter on '{key}' requires a 'value'")
    where = f"filter '{key}'"
    value = (_check_kind(raw["value"], where) if key == "kind"
             else _check_scalar(raw["value"], where))
    return {"type": ftype, "key": key, "value": value}
