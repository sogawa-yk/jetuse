"""MVP 契約スキーマ(`jetuse_platform/contracts/schemas/`)のローダー。

スキーマ JSON はパッケージに同梱され(`pip install` で wheel/イメージに入る)、
`importlib.resources` で読む。import 時にはファイル IO せず、初回検証時に遅延読込する。
新規依存は足さず、既存の `jsonschema` (Draft 2020-12) を使う。

公開 API(`load_schema`)はキャッシュを汚染させないため毎回ディープコピーを返す。
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import datetime
from functools import cache
from importlib.resources import files

from jsonschema import Draft202012Validator, FormatChecker

# RFC 3339 date-time(full-date "T" full-time, tz は Z または ±HH:MM 必須)。
# stdlib fromisoformat は基本形式(区切りなし)・週日付・カンマ小数秒など ISO8601 方言も
# 通し、TZ オフセットの分が 60〜99 でも timedelta 正規化して受理してしまうため、新依存を
# 足さずここで妥当域(時 00-23/分 00-59/秒 00-59、TZ 時 00-23/分 00-59)に厳格化する。
_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]([01]\d|2[0-3]):[0-5]\d:[0-5]\d(\.\d+)?"
    r"([Zz]|[+-]([01]\d|2[0-3]):[0-5]\d)$"
)

# 既定 FormatChecker は date-time を検証しない(rfc3339-validator 等が未導入のため)。
_FORMAT_CHECKER = FormatChecker()


@_FORMAT_CHECKER.checks("date-time", raises=(ValueError, TypeError))
def _check_date_time(value: object) -> bool:
    if not isinstance(value, str):
        return True  # 文字列以外は type キーワード側で扱う
    if not _RFC3339_RE.match(value):
        return False
    # 構造が RFC3339 でも実在しない日時(例 13月/25時)は弾く。Z→+00:00 へ正規化。
    # 既知の狭め: RFC3339 は leap second(秒値 60, 例 1990-12-31T23:59:60Z)を許容するが、
    # stdlib datetime.fromisoformat は弾くため本チェッカも受理しない。Run イベントの ts では
    # 実害なし(新依存を足してまで対応しない)。
    norm = value[:10] + "T" + value[11:]  # 区切り 't'→'T'
    norm = norm.replace("Z", "+00:00").replace("z", "+00:00")
    datetime.fromisoformat(norm)  # 不正なら ValueError → conforms False
    return True


@cache
def _load_schema_cached(name: str) -> dict:
    """スキーマ JSON を読み込んでキャッシュする(初回のみ IO)。内部用・破壊厳禁。"""
    resource = files("jetuse_platform.contracts").joinpath("schemas", f"{name}.schema.json")
    return json.loads(resource.read_text(encoding="utf-8"))


def load_schema(name: str) -> dict:
    """`schemas/<name>.schema.json` を dict で返す。

    キャッシュ汚染を避けるため、呼び出しごとに**新しいディープコピー**を返す
    (呼出側が enum 等を破壊しても内部キャッシュや他の検証に波及しない)。
    """
    return deepcopy(_load_schema_cached(name))


def get_validator(name: str) -> Draft202012Validator:
    """スキーマ名から Draft 2020-12 検証器を返す。format も実検証する。

    検証器は**呼び出しごとに新規構築**する(`@cache` しない)。`validator.schema` は可変で、
    共有すると利用者の書き換えが後続の全検証を恒久汚染するため。スキーマは
    `_load_schema_cached` の deepcopy を使う(IO はキャッシュ・構築は安価)。
    """
    schema = deepcopy(_load_schema_cached(name))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=_FORMAT_CHECKER)
