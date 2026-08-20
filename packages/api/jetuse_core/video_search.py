"""場面の横断検索(VID-04 / specs/20 §4)。

**返すのは場面**。映像単位に丸めない(tasks/VID-04 禁止事項) —— 利用者が探しているのは
「どの映像か」ではなく「どの瞬間か」で、映像に丸めた瞬間にその情報が失われる。

**ベクトル距離とメタデータ条件を同一の SQL で評価する**(ADR-0032 決定4)。実 ADB で
`VECTOR_DISTANCE(embedding, :q, COSINE)` と `WHERE` が 1 クエリで効くことを実測済み
(比較ドキュメント §5.5)。要求4(自然言語)と要求5(条件絞り込み)を別々の検索系に
またがらせない。

**しきい値で足切りしない。順位で返す。** 実測(2026-08-20)では「豪雨」に対し正解の
雨天場面が 0.501、無関係な場面が 0.408 で、**差は 0.09 程度しかない**。絶対値で切ると
無関係を通すか正解を落とすかのどちらかになる(比較ドキュメント §5.5)。

**ベクトル索引(IVF / HNSW)は張らない**(ADR-0032「未検証として残すもの」)。件数が
少ないうちは要らず、要否は測ってから決める。よって `FETCH APPROX FIRST` ではなく
素の `FETCH FIRST`(厳密検索)を使う —— 索引が無いのに APPROX と書くと、読んだ人に
「索引がある前提の SQL」と誤解させる。

**根拠は必ず返す**(要求11)。なぜその場面が出たのかを利用者が確認できることは要件で
あり、AI 検索をブラックボックスにしないという設計方針そのもの。理由文が空になる経路を
作らない(`_reason` の最後に必ず落ちる文がある)。

所有者分離は既存の流儀に合わせ、**SQL の `WHERE va.owner_sub = :owner` で強制**する。
"""

import array
import json
import logging
import re
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_futures
from datetime import UTC, date, datetime, timedelta
from typing import Any

from . import video
from .db import connect
from .video_analyze import INDOOR_VALUES, TIME_OF_DAY_VALUES, UNKNOWN

logger = logging.getLogger("jetuse.video_search")

# 1 回の検索で返す場面数。天井超過はクランプする(`video.list_assets` と同じ扱い ——
# 取得件数が減るだけで検索の意味は壊れない)。
DEFAULT_LIMIT = 20
LIMIT_MAX = 100

# 条件値の上限。列幅(最長 `rights VARCHAR2(1000)`)ではなく**入力の上限**として置く。
# これを超える値は一覧から選んだ値ではありえない(要求5 は「一覧から条件を選択」)。
VALUE_MAX = 400
# 1 回の検索で指定できるタグ数。すべてを満たす場面に絞る(AND)。
TAGS_MAX = 10

# NUL を含む値を弾く(`rag_adb._SAFE_VALUE` と同じ考え)。値はすべてバインドするので
# SQL の構文には触れないが、NUL は Oracle 側で扱いが分かれるので入口で落とす。
_SAFE_VALUE = re.compile(rf"^[^\x00]{{1,{VALUE_MAX}}}$")

# バインド名の接頭辞。キーをそのままバインド名にしない(`:rights` のような語が
# 予約語とぶつかると ORA-01745 になる。`rag_adb.BIND_PREFIX` と同じ理由)。
BIND_PREFIX = "flt_"

_FROM = "video_scenes vs JOIN video_assets va ON va.id = vs.asset_id"

_TS = "TO_CHAR({col}, 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')"

# 返す列。**場面の列と、その場面が属する映像の基本情報**(specs/20 §4 の hits)。
_HIT_COLUMNS = f"""
    vs.id AS scene_id, vs.asset_id AS asset_id, vs.start_ms AS start_ms,
    vs.end_ms AS end_ms, vs.description AS description, vs.tags AS tags,
    vs.objects AS objects, vs.people AS people, vs.actions AS actions,
    vs.place AS place, vs.scene_kind AS scene_kind, vs.indoor AS indoor,
    vs.time_of_day AS time_of_day, vs.weather AS weather,
    vs.screen_text AS screen_text, vs.thumb_object AS thumb_object,
    vs.source AS source, {_TS.format(col="vs.confirmed_at")} AS confirmed_at,
    va.title AS title, va.collection AS collection, va.category AS category,
    va.rights AS rights, va.duration_ms AS duration_ms,
    va.analysis_state AS analysis_state,
    {_TS.format(col="va.captured_at")} AS captured_at,
    {_TS.format(col="va.created_at")} AS created_at"""

# 完全一致で絞る条件 → 列。**部分一致にしない** —— 要求5 は「一覧から条件を選択して
# 探せる」で、値は画面が持っている選択肢から来る。LIKE にすると `%` `_` の
# エスケープを持ち込むわりに、選択肢からの絞り込みには効かない。
_EXACT_FILTERS = {
    "collection": "va.collection",   # 所属
    "category": "va.category",       # カテゴリ
    "rights": "va.rights",           # 権利範囲
    "place": "vs.place",             # 場所
}

# 集合が決まっている条件 → (列, 許される値)。DB の CHECK 制約と同じ集合を使い回す
# (綴り違いを 0 件ではなく 422 で返す)。
_ENUM_FILTERS = {
    "indoor": ("vs.indoor", INDOOR_VALUES),                 # 屋内外
    "time_of_day": ("vs.time_of_day", TIME_OF_DAY_VALUES),  # 昼夜
    "analysis_state": ("va.analysis_state", video.ANALYSIS_STATES),  # 分析状態
}

# 期間で絞る条件 → (列, 下限か)。**撮影日と登録日**(tasks/VID-04 の条件一覧)。
_DATE_FILTERS = {
    "captured_from": ("va.captured_at", True),
    "captured_to": ("va.captured_at", False),
    "created_from": ("va.created_at", True),
    "created_to": ("va.created_at", False),
}

# 尺(**映像の長さ**。specs/20 §4 の例 `duration_max_ms: 600000` = 10 分は映像の長さで、
# 場面の長さではない。場面は最長 30 秒に割ってある = video_frames)。
_DURATION_FILTERS = {
    "duration_min_ms": ("va.duration_ms", ">="),
    "duration_max_ms": ("va.duration_ms", "<="),
}

FILTER_KEYS = frozenset(
    set(_EXACT_FILTERS) | set(_ENUM_FILTERS) | set(_DATE_FILTERS)
    | set(_DURATION_FILTERS) | {"has_people", "tags", "confirmed"}
)

# 根拠(要求11)に出す日本語のラベル。**利用者が読む文**なので列名を出さない。
_LABELS = {
    "captured_from": "撮影日", "captured_to": "撮影日",
    "created_from": "登録日", "created_to": "登録日",
    "collection": "所属", "category": "カテゴリ", "rights": "権利範囲",
    "place": "場所", "indoor": "屋内外", "time_of_day": "昼夜",
    "has_people": "人物", "tags": "タグ", "confirmed": "確認",
    "duration_min_ms": "尺", "duration_max_ms": "尺",
    "analysis_state": "分析状態",
}

# 根拠の `fields`(効いた項目)に出す列名。ラベルと違い**機械が使う**(画面のハイライト)。
_FIELDS = {
    "captured_from": "captured_at", "captured_to": "captured_at",
    "created_from": "created_at", "created_to": "created_at",
    "collection": "collection", "category": "category", "rights": "rights",
    "place": "place", "indoor": "indoor", "time_of_day": "time_of_day",
    "has_people": "people", "tags": "tags", "confirmed": "confirmed_at",
    "duration_min_ms": "duration_ms", "duration_max_ms": "duration_ms",
    "analysis_state": "analysis_state",
}

_VALUE_LABELS = {
    "indoor": {"indoor": "屋内", "outdoor": "屋外", UNKNOWN: "屋内外不明"},
    "time_of_day": {"day": "昼", "night": "夜", UNKNOWN: "昼夜不明"},
}

# 検索語と照合する場面の項目。**短い語の項目だけ**を語単位で照合する
# (`description` / `screen_text` は自由文なので `_LONG_TEXT_FIELDS` で別に扱う)。
_LEXICAL_LIST_FIELDS = ("tags", "objects", "actions")
_LEXICAL_SCALAR_FIELDS = ("place", "scene_kind", "weather")
_LONG_TEXT_FIELDS = ("description", "screen_text")

# サムネイル URL(PAR)の寿命と、期限間際の URL を配らないための余白。
THUMB_TTL_SECONDS = 3600
_THUMB_MARGIN_SECONDS = 300
_THUMB_PAR_PREFIX = "jetuse-video-thumb-"
# PAR の発行は 1 件ずつ REST 往復する。検索 1 回ぶん(最大 LIMIT_MAX 件)を直列に
# 発行すると検索の応答時間がそのまま伸びるので、少しだけ重ねる
# (`video_analyze.DESCRIBE_CONCURRENCY` と同じ考え)。
_THUMB_CONCURRENCY = 8
# 発行済み PAR の使い回し。**同じサムネイルに検索のたびに PAR を作らない** ——
# PAR はバケットに溜まり、映像を消すまで消えない(`video._purge_objects`)。
_thumb_cache: dict[str, tuple[str, datetime]] = {}
_thumb_lock = threading.Lock()
# 発行中のサムネイル。**並行した検索が同じ object の PAR を二重に作らないため**の
# 引換券(Future)を持つ。一覧で多数のサムネイルを引く画面(VID-06)が一番踏みやすい。
_thumb_inflight: dict[str, "Future[str | None]"] = {}
# 先行する発行を待つ上限。**検索 1 回ぶんの合計**であって 1 件あたりではない ——
# 1 件ずつ上限を掛けると `20 秒 × 件数`(limit=100 なら 30 分超)まで伸び、
# 「待ちを有限にして検索を固めない」という狙いが成立しない。待ちきれなかった
# サムネイルは諦める(URL が欠けるだけ。次の検索ではキャッシュから取れる)。
_THUMB_WAIT_SECONDS = 20


class SearchInputError(ValueError):
    """検索条件が受け取れない(未知のキー・集合外の値・長すぎる値)。API は 422。

    **未知のキーを黙って捨てない**(`rag_adb.build_where` と同じ)。誤字が静かに
    「条件なしの全件」になると、利用者は絞り込めたつもりで別のものを見る。
    """


class SearchBackendError(RuntimeError):
    """検索語を埋め込みにできなかった(認証・429・タイムアウト・サービス障害)。

    **利用者の入力の問題ではない**ので 422 にしない(API は 502)。映像や検索語を
    変えても直らないものを、利用者に直させようとしないため。

    **上流の例外文字列をそのまま持たない。** OCI SDK の例外は内部エンドポイント・
    request id・構成値を含みうる(実測: `opc-request-id` 付きの dict がそのまま
    文字列になる)。API はこの文言をそのまま 502 の detail に載せるので、
    詳細はサーバ側のログにだけ残す。
    """


# --- 条件 ---------------------------------------------------------------------


def _text_value(key: str, value: Any) -> str:
    if not isinstance(value, str) or not _SAFE_VALUE.match(value):
        raise SearchInputError(f"invalid filter value for {key}")
    return value


def _bool_value(key: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise SearchInputError(f"{key} must be a boolean")
    return value


def _limit_value(value: Any) -> int:
    """返す件数。**`int()` で変換しない**(API スキーマと違う規則を core に作らない)。

    `int(True)` は 1 なので、変換で受けると `limit: true` が「1 件」として通る。
    ワイヤの入口は `VideoSearchRequest.limit`(StrictInt)が弾くが、core を直接呼ぶ
    経路もあるので**同じ規則をここにも置く**。範囲外はクランプする(取得件数が減る
    だけで検索の意味は壊れない。`video.list_assets` と同じ扱い)。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise SearchInputError("limit must be an integer")
    return max(1, min(value, LIMIT_MAX))


def _int_value(key: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SearchInputError(f"{key} must be an integer")
    if value < 0:
        raise SearchInputError(f"{key} must not be negative")
    return value


def _time_bound(key: str, value: Any, *, lower: bool) -> tuple[str, datetime]:
    """期間の端を (比較演算子, 値) にする。**日付だけの指定はその日を丸ごと含める**。

    `captured_to: "2026-12-31"` を `<= 2026-12-31T00:00` と読むと、その日に撮った
    映像がほとんど落ちる(利用者から見れば「12/31 まで」と指定したのに 12/31 が出ない)。
    日付だけを渡されたときは**翌日 00:00 未満**として扱う。時刻まで指定された場合は
    その時刻ちょうどまで(`<=`)—— こちらは利用者が境界を明示している。

    保存側は UTC の `TIMESTAMP`(`video.to_utc_naive`)なので、ここでも UTC へ寄せる。
    タイムゾーンを付けずに渡された値は UTC として扱う(保存側と同じ規則)。
    """
    if isinstance(value, datetime):
        return (">=" if lower else "<="), video.to_utc_naive(value)
    if isinstance(value, date):
        day = datetime(value.year, value.month, value.day)
        return (">=", day) if lower else ("<", day + timedelta(days=1))
    raw = _text_value(key, value).strip()
    date_only = len(raw) == 10
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as e:
        raise SearchInputError(f"{key} must be ISO-8601: {raw[:40]}") from e
    parsed = video.to_utc_naive(parsed)
    if date_only:
        return (">=", parsed) if lower else ("<", parsed + timedelta(days=1))
    return (">=" if lower else "<="), parsed


def _tags_value(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise SearchInputError("tags must be a list of strings")
    if len(value) > TAGS_MAX:
        raise SearchInputError(f"too many tags (max {TAGS_MAX})")
    tags = [_text_value("tags", t).strip() for t in value]
    return [t for t in tags if t]


def build_where(
    owner: str, filters: dict[str, Any] | None
) -> tuple[str, dict[str, Any], list[tuple[str, Any]]]:
    """許可キーだけを WHERE に組み、**値はすべてバインド**する。

    戻り値は (WHERE 句, バインド, 効いた条件の一覧)。3 つ目は根拠(要求11)を作るために
    返す —— 「どの条件が効いたか」を後から SQL 文字列から復元しようとすると、
    組み立てと解釈の 2 か所を揃え続けることになる。

    所有者の条件は**呼び出し側の指定に関わらず必ず入る**。
    """
    clauses = ["va.owner_sub = :owner"]
    binds: dict[str, Any] = {"owner": owner}
    applied: list[tuple[str, Any]] = []

    for key, value in (filters or {}).items():
        if key not in FILTER_KEYS:
            raise SearchInputError(f"unsupported filter key: {key}")
        if value is None:
            continue
        name = f"{BIND_PREFIX}{key}"

        if key in _EXACT_FILTERS:
            binds[name] = _text_value(key, value)
            clauses.append(f"{_EXACT_FILTERS[key]} = :{name}")
        elif key in _ENUM_FILTERS:
            col, allowed = _ENUM_FILTERS[key]
            text = _text_value(key, value)
            if text not in allowed:
                raise SearchInputError(
                    f"{key} must be one of {sorted(allowed)} (got {text!r})"
                )
            binds[name] = text
            clauses.append(f"{col} = :{name}")
            value = text
        elif key in _DATE_FILTERS:
            col, lower = _DATE_FILTERS[key]
            op, bound = _time_bound(key, value, lower=lower)
            binds[name] = bound
            # 撮影日が NULL(= 不明。specs/20 §1)の映像はどの期間にも入らない。
            # 「不明」を勝手にどこかの期間へ寄せない
            clauses.append(f"{col} {op} :{name}")
            # **根拠に出すのは利用者が指定した値**(`value` のまま applied へ)。
            # 内部境界(`bound`)を渡すと、`captured_to="2026-12-31"` の理由が
            # 「撮影日 2027-01-01 まで」になり、指定と 1 日ずれて見える
        elif key in _DURATION_FILTERS:
            col, op = _DURATION_FILTERS[key]
            binds[name] = _int_value(key, value)
            clauses.append(f"{col} {op} :{name}")
        elif key == "has_people":
            # `people` は {"present": "yes|no|unknown"}(video_analyze._people)。
            # **`unknown` はどちらにも入れない** —— 判らないものを「人物なし」に
            # 寄せると、要求2 の「不明として扱える」が結果に出なくなる
            binds[name] = "yes" if _bool_value(key, value) else "no"
            clauses.append(f"JSON_VALUE(vs.people, '$.present') = :{name}")
        elif key == "confirmed":
            # 人が確認したかどうかは `confirmed_at` が持つ(specs/20 §1)。
            # `source` ではなく時刻で見る —— 修正(`human`)と確認(`ai_confirmed`)の
            # どちらでも「確認した時刻」が入る
            clauses.append(
                "vs.confirmed_at IS NOT NULL" if _bool_value(key, value)
                else "vs.confirmed_at IS NULL"
            )
        elif key == "tags":
            tags = _tags_value(value)
            if not tags:
                continue
            # **指定したタグをすべて持つ場面**に絞る(AND)。JSON パスは定数で、
            # 値は PASSING でバインドする(値が SQL にも JSON パスにも混ざらない)
            for i, tag in enumerate(tags):
                tag_bind = f"{name}{i}"
                binds[tag_bind] = tag
                clauses.append(
                    f"JSON_EXISTS(vs.tags, '$[*]?(@ == $t)' PASSING :{tag_bind} AS \"t\")"
                )
            value = tags
        applied.append((key, value))

    return "WHERE " + " AND ".join(clauses), binds, applied


# --- SQL ----------------------------------------------------------------------


def ranked_sql(where: str, *, exclude_self: bool = False) -> str:
    """距離と条件を**同一の SQL** で評価する(ADR-0032 決定4)。

    ベクトルの無い場面は順位を付けられないので結果からは外すが、**黙って落とさない**
    —— 条件には一致したのに外した件数を `no_vector_count` として同じクエリで数え、
    応答に載せる(`excluded_no_vector`)。別クエリで数えると、条件を 2 か所に
    書くことになって食い違う。

    **外すのは SQL ではなく呼び出し側**(`has_vector = 1` を WHERE に書かない)。
    書くと、条件に合う場面が全部ベクトル無しだったときに 1 行も返らず、
    `no_vector_count` ごと消えてしまう ——「該当が無い」のか「まだ分析されていない」
    のかを分けるための数字が、いちばん必要な場面で失われる。ベクトルの無い行は
    `has_vector DESC` で必ず後ろに寄るので、順位付きの結果が先に埋まる。

    **件数は `FETCH FIRST` で切る前を数える**(`COUNT(*) OVER ()`)。分析関数は行を
    絞る前に評価されるので、`limit` で切っても全一致件数が残る。返した行数を件数として
    使うと、1000 件一致・`limit=20` の検索が画面に「20 件中 1 位」と出る ——
    **画面の件数が嘘になる**(VID-04 の指摘を VID-06 で修正)。
    """
    self_clause = " AND vs.id <> :self_id" if exclude_self else ""
    return f"""
SELECT {_HIT_COLUMNS},
       CASE WHEN vs.embedding IS NULL THEN 0 ELSE 1 END AS has_vector,
       VECTOR_DISTANCE(vs.embedding, :q, COSINE) AS distance,
       COUNT(CASE WHEN vs.embedding IS NULL THEN 1 END) OVER () AS no_vector_count,
       COUNT(*) OVER () AS match_count
  FROM {_FROM}
{where}{self_clause}
ORDER BY has_vector DESC, distance, vs.id
FETCH FIRST :lim ROWS ONLY
"""


def filter_sql(where: str) -> str:
    """条件だけの絞り込み(要求5)。**距離が無いので順位は時系列**。

    ベクトルの有無で場面を外さない —— 分析が終わっていない場面も、条件に合うなら
    一覧に出るのが自然(「一覧から条件を選択して探せる」)。

    件数(`match_count`)は `ranked_sql` と同じく**切る前**を数える。画面は
    「全 N 件中 M 件を表示」を出すので、返した行数を件数にすると嘘になる。
    """
    return f"""
SELECT {_HIT_COLUMNS},
       1 AS has_vector,
       CAST(NULL AS NUMBER) AS distance,
       0 AS no_vector_count,
       COUNT(*) OVER () AS match_count
  FROM {_FROM}
{where}
ORDER BY va.created_at DESC, vs.asset_id, vs.start_ms
FETCH FIRST :lim ROWS ONLY
"""


# --- 根拠(要求11) -------------------------------------------------------------


def _as_object(value: Any) -> Any:
    """JSON オブジェクト列(`people`)を dict にする。**壊れていても検索を落とさない**。

    リストと同じく、oracledb は `IS JSON` 制約から Python の値で返すことも文字列で
    返すこともある。応答の型を呼び出し側で分岐させないため、ここで揃える。
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _as_list(value: Any) -> list[str]:
    """JSON 列を list にする。**壊れていても検索を落とさない**。

    python-oracledb は `IS JSON` 制約から JSON と判ると Python の値で返し、
    そうでなければ文字列で返す。どちらでも同じに扱う。
    """
    if isinstance(value, list):
        return [str(v) for v in value if isinstance(v, str | int | float)]
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return []
        return [str(v) for v in parsed] if isinstance(parsed, list) else []
    return []


def _lexical_hit(term: str, query: str) -> bool:
    """検索語と場面の語が字面で当たっているか。

    形態素解析は持ち込まない(この 1 機能のために辞書を積まない)。短い語どうしの
    包含で見る —— 「豪雨」で検索したとき、タグ「雨」は `雨 in 豪雨` で当たる。
    **1 文字の ASCII は見ない**(`a` のような語がどんな検索語にも当たってしまう)。
    """
    term = term.strip()
    if not term or term == UNKNOWN:
        return False
    if term.isascii() and len(term) < 2:
        return False
    lower_term, lower_q = term.lower(), query.lower()
    return lower_term in lower_q or lower_q in lower_term


def _lexical_matches(row: dict[str, Any], query: str) -> tuple[list[str], list[str]]:
    """検索語と字面で一致した (項目, タグ) を返す。

    ベクトル検索は**意味**で引くので、字面が一致しないことは普通にある(それが狙い)。
    一致したときだけ「どの項目に当たったか」を根拠に足し、無ければ距離と順位で語る。
    ここで無理に項目名をひねり出すと、**効いていない項目を効いたことにしてしまう**。
    """
    fields: list[str] = []
    tags: list[str] = []
    for key in _LEXICAL_LIST_FIELDS:
        hit = [t for t in _as_list(row.get(key)) if _lexical_hit(t, query)]
        if hit:
            fields.append(key)
            if key == "tags":
                tags += hit
    for key in _LEXICAL_SCALAR_FIELDS:
        value = row.get(key)
        if isinstance(value, str) and _lexical_hit(value, query):
            fields.append(key)
    # 自由文は語単位に切れないので、**検索語がそのまま含まれるとき**だけ見る
    for key in _LONG_TEXT_FIELDS:
        value = row.get(key)
        if isinstance(value, str) and len(query) >= 2 and query.lower() in value.lower():
            fields.append(key)
    return fields, tags


def _filter_phrase(key: str, value: Any) -> str:
    """効いた条件 1 つを、利用者が読める句にする。"""
    label = _LABELS[key]
    if key in _VALUE_LABELS:
        return _VALUE_LABELS[key].get(value, f"{label}={value}")
    if key == "has_people":
        return "人物あり" if value == "yes" or value is True else "人物なし"
    if key == "confirmed":
        return "確認済み" if value else "未確認"
    if key in _DATE_FILTERS:
        # 利用者が渡した値をそのまま見せる(時刻まで指定されたならそれも出す)
        if isinstance(value, datetime):
            stamp = value.strftime(
                "%Y-%m-%d" if (value.hour, value.minute, value.second) == (0, 0, 0)
                else "%Y-%m-%d %H:%M"
            )
        elif isinstance(value, date):
            stamp = value.strftime("%Y-%m-%d")
        else:
            stamp = str(value).strip()
        return f"{label} {stamp} {'以降' if _DATE_FILTERS[key][1] else 'まで'}"
    if key in _DURATION_FILTERS:
        edge = "以上" if key == "duration_min_ms" else "以下"
        return f"{label} {value / 1000:.1f} 秒{edge}"
    if key == "tags":
        return "タグ " + "・".join(f"「{t}」" for t in value)
    return f"{label}={value}"


def _reason(
    row: dict[str, Any],
    *,
    applied: list[tuple[str, Any]],
    query: str | None,
    similar_of: str | None,
    rank: int,
    total: int,
) -> dict[str, Any]:
    """**必ず中身のある根拠**を返す(要求11 / tasks/VID-04「空の理由文を返さない」)。

    根拠は 3 種類の材料からできている。どれも無い検索(条件も検索語も無い一覧)でも、
    最後に「並び順そのもの」を理由として返す —— 空文字を返す経路を作らない。
    """
    distance = row.get("distance")
    fields: list[str] = []
    tags: list[str] = []
    sentences: list[str] = []

    if query:
        hit_fields, hit_tags = _lexical_matches(row, query)
        fields += hit_fields
        tags += hit_tags
        place = f"{total} 件中 {rank} 位"
        if distance is None:  # 起こらないはずだが、根拠を空にしない
            sentences.append(f"「{query}」で検索した結果の{place}です")
        else:
            sentences.append(
                f"「{query}」に意味が近い場面です(距離 {distance:.3f}・{place})"
            )
        if hit_tags:
            sentences.append(
                "検索語と同じ語のタグ" + "・".join(f"「{t}」" for t in hit_tags) + "が付いています"
            )
        elif hit_fields:
            sentences.append(
                "検索語と同じ語が" + "・".join(hit_fields) + "にあります"
            )
    elif similar_of:
        place = f"{total} 件中 {rank} 位"
        near = f"(距離 {distance:.3f}・{place})" if distance is not None else f"({place})"
        sentences.append(f"指定した場面に内容が近い場面です{near}")

    if applied:
        phrases = [_filter_phrase(key, value) for key, value in applied]
        fields += [_FIELDS[key] for key, _ in applied]
        for key, value in applied:
            if key == "tags":
                tags += list(value)
        sentences.append("条件(" + "・".join(phrases) + ")に一致しています")

    if not sentences:
        # 条件も検索語も無い = 一覧。**それでも理由を返す**(なぜこの並びなのか)
        sentences.append("条件を指定していないため、登録の新しい順に並べています")

    return {
        "reason": "。".join(sentences),
        "fields": list(dict.fromkeys(fields)),
        "tags": list(dict.fromkeys(tags)),
        "distance": round(distance, 4) if distance is not None else None,
    }


# --- サムネイル URL -----------------------------------------------------------


def _thumb_urls(objects: list[str]) -> dict[str, str]:
    """サムネイルの期限付き URL(PAR)をまとめて用意する(specs/20 §4 の `thumb_url`)。

    **発行済みの PAR を使い回す。** PAR はバケットに溜まり、映像を消すまで消えない
    (`video._purge_objects` がまとめて消す)。検索のたびに作ると、同じサムネイルに
    対する PAR が検索回数ぶん積み上がる。

    **並行した検索も 1 回に畳む**(VID-04 の指摘 / VID-06 で修正)。発行中の object は
    引換券(`Future`)を `_thumb_inflight` に置き、後から来た検索はそれを待つ。以前は
    キャッシュの確認と登録だけをロックで守っており、同じサムネイルを含む検索が並行
    すると**その回数だけ PAR が積み上がった** —— 一覧で多数のサムネイルを引く画面が
    一番踏みやすい。待ちが増えるのを嫌って畳まない選択もあり得たが、待つ相手は
    「自分が出すはずだった REST 往復」そのものなので、待っても遅くならない。

    **待ちは有限**(`_THUMB_WAIT_SECONDS`)。先行が異常に遅ければそのサムネイルだけ
    諦める(URL が 1 つ欠けるだけで、検索結果は返る)。

    **ここが落ちても検索は返す。** 検索の本体は DB で完結しており、サムネイルが
    出ないことと場面が見つからないことは別 —— Object Storage 側の不調で
    「検索できない」にしない(理由はログに残す)。
    """
    now = datetime.now(UTC)
    fresh = now + timedelta(seconds=_THUMB_MARGIN_SECONDS)
    out: dict[str, str] = {}
    todo: list[str] = []
    waiting: dict[str, Future[str | None]] = {}
    with _thumb_lock:
        for name in {n for n in objects if n}:
            hit = _thumb_cache.get(name)
            if hit and hit[1] > fresh:
                out[name] = hit[0]
            elif name in _thumb_inflight:
                waiting[name] = _thumb_inflight[name]  # 先行の発行を待つ(作らない)
            else:
                todo.append(name)
                _thumb_inflight[name] = Future()
        # 期限切れを溜めない(検索した場面のぶんだけ増え続けないように)
        for name, (_, expires) in list(_thumb_cache.items()):
            if expires <= now:
                del _thumb_cache[name]
    if not todo:
        _collect_waiting(waiting, out)
        return out

    try:
        import oci.object_storage.models as osm

        bucket = video.require_bucket()
        client = video.os_client()
        ns = client.get_namespace().data
        expires = now + timedelta(seconds=THUMB_TTL_SECONDS)
        region = video.get_settings().oci_region

        def issue(name: str) -> tuple[str, str] | None:
            try:
                par = client.create_preauthenticated_request(
                    ns, bucket,
                    osm.CreatePreauthenticatedRequestDetails(
                        name=f"{_THUMB_PAR_PREFIX}{name.rsplit('/', 1)[-1]}",
                        object_name=name,
                        access_type="ObjectRead",
                        time_expires=expires,
                    ),
                ).data
            except Exception:  # noqa: BLE001
                logger.exception("video thumbnail PAR failed (ignored): %s", name)
                return None
            url = getattr(par, "full_path", None) or (
                f"https://objectstorage.{region}.oraclecloud.com{par.access_uri}"
            )
            return name, url

        with ThreadPoolExecutor(max_workers=min(_THUMB_CONCURRENCY, len(todo))) as pool:
            issued = [r for r in pool.map(issue, todo) if r]
        with _thumb_lock:
            for name, url in issued:
                _thumb_cache[name] = (url, expires)
                out[name] = url
    except Exception:  # noqa: BLE001
        logger.exception("video thumbnail URLs unavailable (ignored)")
    finally:
        # **引換券を必ず片付ける。** 途中で落ちても、待っている検索を宙吊りにしない
        # (次の検索がやり直せるよう、発行できなかった object は in-flight から外す)
        _release_inflight(todo, out)
    _collect_waiting(waiting, out)
    return out


def _release_inflight(names: list[str], issued: dict[str, str]) -> None:
    """自分が握った引換券を、結果(または失敗の None)を入れて手放す。"""
    with _thumb_lock:
        for name in names:
            future = _thumb_inflight.pop(name, None)
            if future is not None and not future.done():
                future.set_result(issued.get(name))


def _collect_waiting(
    waiting: dict[str, "Future[str | None]"], out: dict[str, str]
) -> None:
    """先行する発行の結果を受け取る。**待ちきれない/失敗はそのまま欠かす**。

    待つのは**全体で 1 回**(`wait` に全 Future をまとめて渡す)。1 件ずつ
    `future.result(timeout=...)` を呼ぶと上限が件数ぶん積み上がる。
    """
    if not waiting:
        return
    wait_futures(list(waiting.values()), timeout=_THUMB_WAIT_SECONDS)
    for name, future in waiting.items():
        if not future.done():
            logger.warning("video thumbnail wait gave up (ignored): %s", name)
            continue
        try:
            url = future.result()
        except Exception:  # noqa: BLE001 — 理由はログにだけ残す
            logger.warning("video thumbnail wait failed (ignored): %s", name)
            continue
        if url:
            out[name] = url


# --- 検索 ---------------------------------------------------------------------


def _scene_vector(owner: str, scene_id: str) -> Any:
    """類似検索の起点(要求10)。**その場面のベクトルをそのまま第2引数に渡す**。

    比較ドキュメント §5.5 の実測どおり、追加の仕組みは要らない。所有者の映像に
    属する場面でなければ `LookupError`(他人に id の存在有無を漏らさない)。
    """
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT vs.embedding FROM video_scenes vs"
            " JOIN video_assets va ON va.id = vs.asset_id"
            " WHERE vs.id = :id AND va.owner_sub = :o",
            id=scene_id, o=owner,
        )
        row = cur.fetchone()
    if row is None:
        raise LookupError(scene_id)
    if row[0] is None:
        raise SearchInputError(
            "この場面はまだベクトルが無いため類似検索に使えません(分析が必要です)"
        )
    return row[0]


def _query_vector(query: str) -> Any:
    from .embeddings import embed

    try:
        vectors = embed([query], input_type="SEARCH_QUERY")
    except Exception as e:  # noqa: BLE001
        # 原因はログにだけ残す(クライアントへは固定の文言。SearchBackendError の docstring)
        logger.exception("video search embedding failed")
        raise SearchBackendError(
            "検索語をベクトルに変換できませんでした。時間をおいて試してください"
        ) from e
    if not vectors:
        raise SearchBackendError("検索語をベクトルに変換できませんでした(応答が空)")
    return array.array("f", vectors[0])


def _hit(row: dict[str, Any], thumbs: dict[str, str], matched: dict[str, Any]) -> dict:
    """1 件の**場面**(specs/20 §4 の hits)。映像単位に丸めない。"""
    return {
        "scene_id": row["scene_id"],
        "asset_id": row["asset_id"],
        "title": row["title"],
        "start_ms": row["start_ms"],
        "end_ms": row["end_ms"],
        "thumb_object": row["thumb_object"],
        "thumb_url": thumbs.get(row["thumb_object"] or ""),
        "description": row["description"],
        "tags": _as_list(row["tags"]),
        "objects": _as_list(row["objects"]),
        "actions": _as_list(row["actions"]),
        "people": _as_object(row["people"]),
        "place": row["place"],
        "scene_kind": row["scene_kind"],
        "indoor": row["indoor"],
        "time_of_day": row["time_of_day"],
        "weather": row["weather"],
        "screen_text": row["screen_text"],
        "source": row["source"],
        "confirmed_at": row["confirmed_at"],
        "matched": matched,
        "asset": {
            "collection": row["collection"],
            "category": row["category"],
            "rights": row["rights"],
            "captured_at": row["captured_at"],
            "created_at": row["created_at"],
            "duration_ms": row["duration_ms"],
            "analysis_state": row["analysis_state"],
        },
    }


def search(
    owner: str,
    *,
    q: str | None = None,
    filters: dict[str, Any] | None = None,
    similar_to_scene_id: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """場面を横断検索する(specs/20 §4)。

    3 つの入口が**同じ 1 本の SQL**に載る(ADR-0032 決定4):

      * `q` … 検索語を埋め込み、距離と条件を同時に評価して**順位**で返す(要求4)
      * `q` 無し … 条件だけの絞り込み(要求5)
      * `similar_to_scene_id` … その場面のベクトルで再検索(要求10)

    `q` と `similar_to_scene_id` の同時指定は受け付けない —— 2 つのベクトルを
    どう混ぜるかは仕様が決めていない。勝手に決めて片方を黙って捨てると、
    利用者は指定したはずの条件が効いていないことに気づけない。
    """
    query = (q or "").strip() or None
    if query and similar_to_scene_id:
        raise SearchInputError(
            "q と similar_to_scene_id は同時に指定できません(どちらか一方)"
        )
    limit = _limit_value(limit)

    where, binds, applied = build_where(owner, filters)
    binds["lim"] = limit

    if similar_to_scene_id:
        binds["q"] = _scene_vector(owner, similar_to_scene_id)
        binds["self_id"] = similar_to_scene_id
        sql = ranked_sql(where, exclude_self=True)
        mode = "similar"
    elif query:
        binds["q"] = _query_vector(query)
        sql = ranked_sql(where)
        mode = "vector"
    else:
        sql = filter_sql(where)
        mode = "filter"

    with connect() as conn:
        cur = conn.cursor()
        cur.execute(sql, **binds)
        cols = [d[0].lower() for d in cur.description]
        fetched = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]

    # ベクトルの無い場面は順位を付けられないので結果から外す。**件数は残す**
    # (`ranked_sql` の docstring)。並びで後ろに寄せてあるので、順位付きの結果は
    # limit まで埋まっている
    excluded = int(fetched[0]["no_vector_count"]) if fetched else 0
    matched = int(fetched[0]["match_count"]) if fetched else 0
    rows = [r for r in fetched if r["has_vector"]]

    thumbs = _thumb_urls([r["thumb_object"] for r in rows])
    # **順位の分母は「返した行数」ではなく全一致件数**(`limit` で切る前)。返した数を
    # 使うと 1000 件一致・limit=20 の検索が「20 件中 1 位」と出て、画面の件数が嘘になる。
    # 順位が付くのはベクトルのある場面だけなので、そのぶんを分母から外す。
    # `max(..., len(rows))` は分析関数が取れない実装(fake / 将来の別バックエンド)でも
    # 「返した件数より小さい分母」という辻褄の合わない数を出さないための下限。
    total = max(matched - excluded, len(rows))
    hits = [
        _hit(row, thumbs, _reason(
            row, applied=applied, query=query,
            similar_of=similar_to_scene_id, rank=i + 1, total=total,
        ))
        for i, row in enumerate(rows)
    ]
    return {
        "mode": mode,
        "hits": hits,
        # 全一致件数(順位を付けられた場面)と、そのうち今回返した件数。画面は
        # 「全 N 件中 M 件を表示」をこの 2 つから出す —— 返した件数だけでは
        # 「これで全部なのか、続きがあるのか」が判らない
        "total": total,
        "returned": len(hits),
        # 条件には一致したが**ベクトルが無くて順位を付けられなかった**件数。
        # 0 件の理由が「該当が無い」のか「まだ分析されていない」のかを分ける
        "excluded_no_vector": excluded,
    }
