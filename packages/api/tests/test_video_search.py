"""VID-04 場面の横断検索。ADB と Object Storage は fake で置き換え、
条件の組み立て / 順位 / 類似検索 / **根拠** / ベクトルが無い場面の扱いを検証する。

**値が SQL 文字列に混ざらないこと**(SQL インジェクション)はここで見る。実 ADB での
距離と条件の同時評価・「豪雨」の意味検索は E2E(`runs/<run-id>/e2e/`)で見る ——
fake の DB は距離を計算できないので、ここで「意味で引けた」とは言えない。
"""

import contextlib
import re
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from jetuse_core import video, video_search
from service.deps import require_video
from service.main import app

client = TestClient(app)

OWNER = "dev-user"

# 本物のサムネイル発行。fixture が差し替える前に控えておく(差し替えを個別に戻すため。
# monkeypatch.undo() は fixture が入れた DB の差し替えごと外してしまい、
# **テストが実 OCI を呼ぶ**)。
_REAL_THUMB_URLS = video_search._thumb_urls
_REAL_QUERY_VECTOR = video_search._query_vector


# --- fake ADB -----------------------------------------------------------------


def _aliases(sql: str) -> list[str]:
    """SQL の select 別名を**そのまま**列名にする。

    fake 側に列の並びを書き写すと、`_HIT_COLUMNS` を足したときに黙ってずれる
    (テストだけが古い形を検証し続ける)。生成された SQL から読む。
    """
    return re.findall(r"\bAS ([a-z_]+)\b", sql)


class FakeCursor:
    def __init__(self, db):
        self.db = db

    def execute(self, sql, **binds):
        self.db["sql"] = " ".join(sql.split())
        self.db["binds"] = binds
        if "SELECT vs.embedding" in sql:  # 類似検索の起点
            scene = self.db["vectors"].get(binds["id"])
            self.rows = [(scene,)] if binds["id"] in self.db["vectors"] else []
            self.cols = ["embedding"]
            return
        self.cols = _aliases(self.db["sql"])
        rows = self.db["rows"]
        self.rows = [tuple(r.get(c) for c in self.cols) for r in rows[: binds["lim"]]]

    @property
    def description(self):
        return [(c.upper(),) for c in self.cols]

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeConn:
    def __init__(self, db):
        self.db = db

    def cursor(self):
        return FakeCursor(self.db)

    def commit(self):
        pass


def scene(**over):
    """1 件ぶんの行。`_HIT_COLUMNS` の別名と同じ鍵で持つ。"""
    row = {
        "scene_id": "s1", "asset_id": "a1", "start_ms": 73000, "end_ms": 91000,
        "description": "傘を差した人物が濡れた路面の前で話している",
        "tags": '["雨", "屋外"]', "objects": '["傘", "路面"]',
        "people": '{"present": "yes", "count": 1}', "actions": '["話している"]',
        "place": "unknown", "scene_kind": "屋外", "indoor": "outdoor",
        "time_of_day": "night", "weather": "雨", "screen_text": None,
        "thumb_object": "video/dev-user/a1/thumb/g1/scene-0001.jpg",
        "source": "ai", "confirmed_at": None, "title": "現場の記録",
        "collection": "設備点検", "category": "点検", "rights": "社内限定",
        "duration_ms": 120000, "analysis_state": "done",
        "captured_at": "2026-08-19T10:00:00Z", "created_at": "2026-08-19T22:00:00Z",
        "has_vector": 1, "distance": 0.501, "no_vector_count": 0,
    }
    row.update(over)
    return row


@pytest.fixture()
def env(monkeypatch):
    db = {"rows": [scene()], "vectors": {}, "sql": "", "binds": {}}

    @contextlib.contextmanager
    def fake_connect():
        yield FakeConn(db)

    monkeypatch.setattr(video_search, "connect", fake_connect)
    monkeypatch.setattr(video_search, "_query_vector", lambda q: f"vec({q})")
    monkeypatch.setattr(video_search, "_thumb_urls", lambda names: {
        n: f"https://par.example/{n}" for n in names if n
    })
    return db


# --- 条件の組み立て(要求5) ----------------------------------------------------


def test_owner_is_always_in_the_where_clause():
    where, binds, applied = video_search.build_where(OWNER, None)
    assert where == "WHERE va.owner_sub = :owner"
    assert binds == {"owner": OWNER}
    assert applied == []


def test_all_filters_combine_into_one_where(env):
    """条件の組み合わせ。**すべて 1 本の SQL の WHERE に載る**(ADR-0032 決定4)。"""
    where, binds, applied = video_search.build_where(OWNER, {
        "captured_from": "2026-01-01", "captured_to": "2026-12-31",
        "created_from": "2026-08-01T00:00:00", "collection": "設備点検",
        "category": "点検", "rights": "社内限定", "place": "大阪",
        "indoor": "outdoor", "time_of_day": "night", "has_people": True,
        "tags": ["雨", "屋外"], "duration_min_ms": 1000,
        "duration_max_ms": 600000, "analysis_state": "done", "confirmed": False,
    })
    assert where.startswith("WHERE va.owner_sub = :owner AND ")
    for fragment in (
        "va.captured_at >= :flt_captured_from",
        "va.captured_at < :flt_captured_to",
        "va.created_at >= :flt_created_from",
        "va.collection = :flt_collection",
        "va.category = :flt_category",
        "va.rights = :flt_rights",
        "vs.place = :flt_place",
        "vs.indoor = :flt_indoor",
        "vs.time_of_day = :flt_time_of_day",
        "JSON_VALUE(vs.people, '$.present') = :flt_has_people",
        "va.duration_ms >= :flt_duration_min_ms",
        "va.duration_ms <= :flt_duration_max_ms",
        "va.analysis_state = :flt_analysis_state",
        "vs.confirmed_at IS NULL",
    ):
        assert fragment in where, fragment
    # タグは指定したぶんだけ AND(すべて持つ場面に絞る)
    assert where.count("JSON_EXISTS(vs.tags") == 2
    assert binds["flt_tags0"] == "雨" and binds["flt_tags1"] == "屋外"
    assert binds["flt_has_people"] == "yes"
    assert [k for k, _ in applied] == [
        "captured_from", "captured_to", "created_from", "collection", "category",
        "rights", "place", "indoor", "time_of_day", "has_people", "tags",
        "duration_min_ms", "duration_max_ms", "analysis_state", "confirmed",
    ]


def test_date_only_upper_bound_includes_that_whole_day():
    """`captured_to: "2026-12-31"` はその日を丸ごと含める(境界で静かに落とさない)。"""
    where, binds, _ = video_search.build_where(OWNER, {"captured_to": "2026-12-31"})
    assert "va.captured_at < :flt_captured_to" in where
    assert binds["flt_captured_to"] == datetime(2027, 1, 1, 0, 0)

    # 時刻まで指定された場合は利用者が境界を明示している = その時刻ちょうどまで
    where, binds, _ = video_search.build_where(
        OWNER, {"captured_to": "2026-12-31T12:00:00"}
    )
    assert "va.captured_at <= :flt_captured_to" in where
    assert binds["flt_captured_to"] == datetime(2026, 12, 31, 12, 0)


def test_offset_aware_bounds_are_converted_to_utc():
    _, binds, _ = video_search.build_where(
        OWNER, {"captured_from": "2026-08-19T10:00:00+09:00"}
    )
    assert binds["flt_captured_from"] == datetime(2026, 8, 19, 1, 0)


def test_unknown_filter_key_is_rejected_not_ignored():
    """誤字を黙って捨てない —— 静かに全件になると絞り込めたつもりで別のものを見る。"""
    with pytest.raises(video_search.SearchInputError, match="unsupported filter key"):
        video_search.build_where(OWNER, {"collectoin": "設備点検"})


def test_enum_values_outside_the_check_constraint_are_rejected():
    with pytest.raises(video_search.SearchInputError, match="indoor must be one of"):
        video_search.build_where(OWNER, {"indoor": "outside"})
    with pytest.raises(video_search.SearchInputError):
        video_search.build_where(OWNER, {"analysis_state": "finished"})


def test_none_values_are_treated_as_unset():
    where, _, applied = video_search.build_where(
        OWNER, {"collection": None, "tags": None}
    )
    assert where == "WHERE va.owner_sub = :owner"
    assert applied == []


def test_wrong_types_are_rejected():
    with pytest.raises(video_search.SearchInputError):
        video_search.build_where(OWNER, {"has_people": "yes"})
    with pytest.raises(video_search.SearchInputError):
        video_search.build_where(OWNER, {"duration_min_ms": "1000"})
    with pytest.raises(video_search.SearchInputError):
        video_search.build_where(OWNER, {"duration_min_ms": -1})
    with pytest.raises(video_search.SearchInputError):
        video_search.build_where(OWNER, {"collection": "x" * 401})
    with pytest.raises(video_search.SearchInputError, match="too many tags"):
        video_search.build_where(OWNER, {"tags": [str(i) for i in range(11)]})


# --- SQL インジェクション -----------------------------------------------------


INJECTIONS = [
    "' OR 1=1 --",
    "x'; DROP TABLE video_scenes; --",
    "設備点検' UNION SELECT id, owner_sub FROM video_assets --",
    '") OR ("a"="a',
]


@pytest.mark.parametrize("payload", INJECTIONS)
def test_filter_values_never_reach_the_sql_text(payload, env):
    """**値はすべてバインド**。SQL 文字列に値が現れないことを実際に確かめる。"""
    video_search.search(
        OWNER, filters={"collection": payload, "place": payload, "tags": [payload]}
    )
    sql = env["sql"]
    assert payload not in sql
    assert "DROP" not in sql.upper()
    assert env["binds"]["flt_collection"] == payload
    assert env["binds"]["flt_tags0"] == payload
    # プレースホルダだけが SQL に載る
    assert "va.collection = :flt_collection" in sql


@pytest.mark.parametrize("payload", INJECTIONS)
def test_injection_in_the_query_text_stays_a_query(payload, env):
    video_search.search(OWNER, q=payload)
    assert payload not in env["sql"]
    assert env["binds"]["q"] == f"vec({payload})"


def test_null_byte_in_a_filter_value_is_rejected():
    with pytest.raises(video_search.SearchInputError):
        video_search.build_where(OWNER, {"collection": "設備\x00点検"})


# --- 自然言語検索(要求4) ------------------------------------------------------


def test_query_search_uses_distance_and_conditions_in_one_sql(env):
    res = video_search.search(
        OWNER, q="豪雨", filters={"indoor": "outdoor", "time_of_day": "night"}
    )
    sql = env["sql"]
    assert "VECTOR_DISTANCE(vs.embedding, :q, COSINE)" in sql
    assert "vs.indoor = :flt_indoor" in sql and "vs.time_of_day = :flt_time_of_day" in sql
    assert "ORDER BY has_vector DESC, distance, vs.id" in sql
    assert res["mode"] == "vector"
    assert len(res["hits"]) == 1


def test_no_threshold_is_applied_to_the_distance(env):
    """**しきい値で切らない**(実測で正解 0.501 / 無関係 0.408 = 差 0.09)。

    遠い場面も候補として返り、順位で並ぶ。絶対値で切ると無関係を通すか正解を落とす。
    """
    env["rows"] = [
        scene(scene_id="near", distance=0.501),
        scene(scene_id="far", distance=0.98, description="スタジオで会話している"),
    ]
    res = video_search.search(OWNER, q="豪雨")
    assert [h["scene_id"] for h in res["hits"]] == ["near", "far"]
    assert [h["matched"]["distance"] for h in res["hits"]] == [0.501, 0.98]
    # 距離に基づく WHERE / HAVING を持たない
    assert "distance <" not in env["sql"] and "distance >" not in env["sql"]


def test_no_vector_index_is_assumed(env):
    """索引を張らないと決めた(ADR-0032 未検証)ので APPROX を書かない。"""
    video_search.search(OWNER, q="豪雨")
    assert "FETCH FIRST :lim ROWS ONLY" in env["sql"]
    assert "APPROX" not in env["sql"]


def test_limit_is_clamped(env):
    video_search.search(OWNER, q="豪雨", limit=10_000)
    assert env["binds"]["lim"] == video_search.LIMIT_MAX
    video_search.search(OWNER, q="豪雨", limit=0)
    assert env["binds"]["lim"] == 1


def test_blank_query_falls_back_to_filter_only(env):
    res = video_search.search(OWNER, q="   ", filters={"indoor": "outdoor"})
    assert res["mode"] == "filter"
    assert "VECTOR_DISTANCE" not in env["sql"]
    assert "ORDER BY va.created_at DESC" in env["sql"]


# --- 条件だけの絞り込み(要求5) ------------------------------------------------


def test_filter_only_search_returns_scenes_with_reasons(env):
    env["rows"] = [scene(distance=None)]
    res = video_search.search(OWNER, filters={"indoor": "outdoor", "tags": ["雨"]})
    assert res["mode"] == "filter"
    hit = res["hits"][0]
    assert hit["matched"]["distance"] is None
    assert "屋外" in hit["matched"]["reason"] and "「雨」" in hit["matched"]["reason"]
    assert set(hit["matched"]["fields"]) == {"indoor", "tags"}
    assert hit["matched"]["tags"] == ["雨"]


def test_filter_only_search_does_not_drop_scenes_without_vectors(env):
    """条件だけの絞り込みはベクトルの有無で場面を外さない(一覧として自然)。"""
    video_search.search(OWNER, filters={"indoor": "outdoor"})
    assert "embedding IS NULL" not in env["sql"]


# --- 0 件 ---------------------------------------------------------------------


def test_zero_hits_returns_an_empty_list_not_an_error(env):
    env["rows"] = []
    res = video_search.search(OWNER, q="豪雨", filters={"place": "存在しない場所"})
    assert res == {"mode": "vector", "hits": [], "excluded_no_vector": 0}


def test_zero_hits_for_filter_only(env):
    env["rows"] = []
    assert video_search.search(OWNER, filters={"category": "無い"})["hits"] == []


# --- ベクトルが無い場面の扱い -------------------------------------------------


def test_scenes_without_vectors_are_excluded_from_ranking_but_counted(env):
    """**黙って落とさない。** 順位は付けられないが、外した件数は返す。"""
    env["rows"] = [
        scene(scene_id="ranked", has_vector=1, distance=0.5, no_vector_count=2),
        scene(scene_id="pending1", has_vector=0, distance=None, no_vector_count=2),
        scene(scene_id="pending2", has_vector=0, distance=None, no_vector_count=2),
    ]
    res = video_search.search(OWNER, q="豪雨")
    assert [h["scene_id"] for h in res["hits"]] == ["ranked"]
    assert res["excluded_no_vector"] == 2


def test_count_survives_when_every_match_lacks_a_vector(env):
    """0 件の理由が「該当が無い」か「まだ分析されていない」かを分ける。

    これが `has_vector = 1` を SQL の WHERE に書かない理由 —— 書くと 1 行も返らず、
    件数ごと消える(いちばん必要な場面で失われる)。
    """
    env["rows"] = [
        scene(scene_id="p1", has_vector=0, distance=None, no_vector_count=3),
        scene(scene_id="p2", has_vector=0, distance=None, no_vector_count=3),
    ]
    res = video_search.search(OWNER, q="豪雨")
    assert res["hits"] == []
    assert res["excluded_no_vector"] == 3


# --- 類似検索(要求10) ---------------------------------------------------------


def test_similar_search_uses_the_scene_vector_and_excludes_itself(env):
    env["vectors"] = {"s1": "raw-vector-of-s1"}
    env["rows"] = [scene(scene_id="s2", distance=0.12)]
    res = video_search.search(OWNER, similar_to_scene_id="s1")
    assert res["mode"] == "similar"
    assert env["binds"]["q"] == "raw-vector-of-s1"
    assert env["binds"]["self_id"] == "s1"
    assert "vs.id <> :self_id" in env["sql"]
    assert "指定した場面に内容が近い" in res["hits"][0]["matched"]["reason"]
    assert res["hits"][0]["matched"]["distance"] == 0.12


def test_similar_search_can_be_combined_with_conditions(env):
    env["vectors"] = {"s1": "raw"}
    video_search.search(
        OWNER, similar_to_scene_id="s1", filters={"collection": "設備点検"}
    )
    assert "va.collection = :flt_collection" in env["sql"]
    assert "VECTOR_DISTANCE" in env["sql"]


def test_similar_search_on_a_missing_scene_is_a_lookup_error(env):
    with pytest.raises(LookupError):
        video_search.search(OWNER, similar_to_scene_id="nope")
    # 所有者の条件つきで引いている(他人の場面を起点にできない)
    assert "va.owner_sub = :o" in env["sql"]


def test_similar_search_on_a_scene_without_a_vector_is_rejected(env):
    env["vectors"] = {"s1": None}
    with pytest.raises(video_search.SearchInputError, match="ベクトルが無い"):
        video_search.search(OWNER, similar_to_scene_id="s1")


def test_query_and_similar_together_are_rejected(env):
    with pytest.raises(video_search.SearchInputError, match="同時に指定できません"):
        video_search.search(OWNER, q="豪雨", similar_to_scene_id="s1")


# --- 根拠(要求11) -------------------------------------------------------------


def test_reason_is_never_empty(env):
    """**空の理由文を返さない**(tasks/VID-04 完了条件)。どの入口でも中身がある。"""
    env["vectors"] = {"s1": "raw"}
    calls = [
        {"q": "豪雨"},
        {"filters": {"indoor": "outdoor"}},
        {"q": "豪雨", "filters": {"time_of_day": "night"}},
        {"similar_to_scene_id": "s1"},
        {},  # 条件も検索語も無い一覧
    ]
    for kwargs in calls:
        res = video_search.search(OWNER, **kwargs)
        for hit in res["hits"]:
            matched = hit["matched"]
            assert matched["reason"].strip(), kwargs
            assert "distance" in matched and "fields" in matched and "tags" in matched


def test_reason_reports_distance_and_rank_for_a_query(env):
    env["rows"] = [scene(scene_id="a", distance=0.501), scene(scene_id="b", distance=0.61)]
    hits = video_search.search(OWNER, q="豪雨")["hits"]
    assert "「豪雨」に意味が近い場面です(距離 0.501・2 件中 1 位)" in hits[0]["matched"]["reason"]
    assert "2 件中 2 位" in hits[1]["matched"]["reason"]


def test_reason_names_the_tag_that_matches_the_query_text(env):
    """「豪雨」で引いたとき、タグ「雨」が当たっていることを根拠に出す。"""
    hit = video_search.search(OWNER, q="豪雨")["hits"][0]
    assert "雨" in hit["matched"]["tags"]
    assert "tags" in hit["matched"]["fields"]
    assert "weather" in hit["matched"]["fields"]  # weather="雨" も「豪雨」に含まれる
    assert "検索語と同じ語のタグ" in hit["matched"]["reason"]


def test_reason_falls_back_to_distance_when_nothing_matches_lexically(env):
    """字面が当たらないのが普通(それが意味検索の狙い)。**項目を捏造しない**。"""
    env["rows"] = [scene(
        tags='["交差点"]', objects='["乗用車"]', actions='["通過する"]',
        weather="unknown", scene_kind="道路", place="unknown",
        description="赤い乗用車が交差点を通過する", distance=0.408,
    )]
    matched = video_search.search(OWNER, q="豪雨")["hits"][0]["matched"]
    assert matched["fields"] == [] and matched["tags"] == []
    assert matched["reason"] == "「豪雨」に意味が近い場面です(距離 0.408・1 件中 1 位)"


def test_reason_lists_the_conditions_that_applied(env):
    matched = video_search.search(OWNER, filters={
        "indoor": "outdoor", "time_of_day": "night", "has_people": True,
        "confirmed": False, "duration_max_ms": 600000,
        "captured_from": "2026-01-01", "collection": "設備点検",
    })["hits"][0]["matched"]
    for phrase in ("屋外", "夜", "人物あり", "未確認", "尺 600.0 秒以下",
                   "撮影日 2026-01-01 以降", "所属=設備点検"):
        assert phrase in matched["reason"], phrase
    assert set(matched["fields"]) == {
        "indoor", "time_of_day", "people", "confirmed_at", "duration_ms",
        "captured_at", "collection",
    }


def test_reason_shows_the_date_the_user_asked_for(env):
    """内部境界(翌日 00:00)ではなく**利用者が指定した日**を根拠に出す。

    `captured_to=2026-12-31` の理由が「2027-01-01 まで」だと、指定と 1 日ずれて見える
    (絞り込みの結果は正しいのに、根拠だけが嘘になる)。
    """
    matched = video_search.search(
        OWNER, filters={"captured_to": "2026-12-31", "captured_from": "2026-01-01"}
    )["hits"][0]["matched"]
    assert "撮影日 2026-12-31 まで" in matched["reason"]
    assert "2027-01-01" not in matched["reason"]
    assert "撮影日 2026-01-01 以降" in matched["reason"]
    # SQL 側は翌日 00:00 未満(境界の扱いは変えない)
    assert env["binds"]["flt_captured_to"] == datetime(2027, 1, 1, 0, 0)


def test_single_ascii_letters_do_not_count_as_a_lexical_match(env):
    """1 文字の ASCII はどんな検索語にも当たる。根拠を薄めないために見ない。"""
    env["rows"] = [scene(tags='["a"]', weather="unknown", scene_kind="unknown")]
    matched = video_search.search(OWNER, q="camera")["hits"][0]["matched"]
    assert matched["tags"] == []


def test_broken_json_in_tags_does_not_break_the_search(env):
    env["rows"] = [scene(tags="{壊れた", objects=None)]
    hit = video_search.search(OWNER, q="豪雨")["hits"][0]
    assert hit["tags"] == [] and hit["objects"] == []
    assert hit["matched"]["reason"]


# --- 返すのは場面(要求6 / 禁止事項) -------------------------------------------


def test_hits_are_scenes_not_assets(env):
    """**映像単位に丸めない。** 同じ映像の別の場面はそれぞれ 1 件として返る。"""
    env["rows"] = [
        scene(scene_id="s1", asset_id="a1", start_ms=0, end_ms=5000, distance=0.4),
        scene(scene_id="s2", asset_id="a1", start_ms=5000, end_ms=9000, distance=0.5),
    ]
    hits = video_search.search(OWNER, q="豪雨")["hits"]
    assert [h["scene_id"] for h in hits] == ["s1", "s2"]
    assert {h["asset_id"] for h in hits} == {"a1"}
    # 場面から再生位置が判る(specs/20 §6 の `?t=<秒>`)
    assert hits[0]["start_ms"] == 0 and hits[0]["end_ms"] == 5000
    # 映像の基本情報は入れ子で添える(場面が主役)
    assert hits[0]["asset"]["collection"] == "設備点検"
    assert hits[0]["title"] == "現場の記録"


def test_hit_carries_the_scene_metadata_and_thumb_url(env):
    hit = video_search.search(OWNER, q="豪雨")["hits"][0]
    assert hit["thumb_url"] == "https://par.example/" + hit["thumb_object"]
    assert hit["tags"] == ["雨", "屋外"] and hit["objects"] == ["傘", "路面"]
    assert hit["indoor"] == "outdoor" and hit["time_of_day"] == "night"
    assert hit["source"] == "ai" and hit["confirmed_at"] is None


def test_json_columns_come_back_as_values_not_strings(env):
    """呼び出し側に JSON の parse をさせない(型を応答で揃える)。"""
    hit = video_search.search(OWNER, q="豪雨")["hits"][0]
    assert hit["people"] == {"present": "yes", "count": 1}
    assert isinstance(hit["tags"], list) and isinstance(hit["actions"], list)

    env["rows"] = [scene(people="{壊れた", tags=None)]
    broken = video_search.search(OWNER, q="豪雨")["hits"][0]
    assert broken["people"] is None and broken["tags"] == []


def test_missing_thumbnail_is_none_not_an_error(env):
    env["rows"] = [scene(thumb_object=None)]
    assert video_search.search(OWNER, q="豪雨")["hits"][0]["thumb_url"] is None


# --- サムネイル URL(PAR) ------------------------------------------------------


class FakePar:
    def __init__(self, name, object_name):
        self.id = name
        self.object_name = object_name
        self.full_path = f"https://objectstorage.example.com/p/tok/{object_name}"


class FakeOS:
    def __init__(self):
        self.created: list[str] = []
        self.fail = False

    def get_namespace(self):
        return SimpleNamespace(data="ns")

    def create_preauthenticated_request(self, ns, bucket, details, **kw):
        if self.fail:
            raise RuntimeError("object storage down")
        self.created.append(details.object_name)
        return SimpleNamespace(data=FakePar(details.name, details.object_name))


@pytest.fixture()
def storage(monkeypatch):
    fake = FakeOS()
    monkeypatch.setattr(video, "os_client", lambda: fake)
    monkeypatch.setattr(video, "require_bucket", lambda: "jetuse-loop-video")
    video_search._thumb_cache.clear()
    yield fake
    video_search._thumb_cache.clear()


def test_thumb_pars_are_issued_once_and_reused(storage):
    names = ["video/o/a1/thumb/g/scene-0001.jpg", "video/o/a1/thumb/g/scene-0002.jpg"]
    first = video_search._thumb_urls(names)
    assert set(first) == set(names)
    assert sorted(storage.created) == sorted(names)

    # 2 回目は作らない(PAR はバケットに溜まり、映像を消すまで消えない)
    second = video_search._thumb_urls(names)
    assert second == first
    assert sorted(storage.created) == sorted(names)


def test_search_still_returns_hits_when_thumbnails_fail(env, storage, monkeypatch):
    """Object Storage の不調で「検索できない」にしない(場面は DB で完結している)。"""
    # env が入れたダミーを**この 1 つだけ**本物へ戻す(DB の差し替えは残す)
    monkeypatch.setattr(video_search, "_thumb_urls", _REAL_THUMB_URLS)
    storage.fail = True
    res = video_search.search(OWNER, q="豪雨")
    assert res["hits"][0]["thumb_url"] is None
    assert res["hits"][0]["matched"]["reason"]


# --- 所有者分離 ---------------------------------------------------------------


def test_owner_is_bound_from_the_caller_not_the_filters(env):
    with pytest.raises(video_search.SearchInputError):
        video_search.search(OWNER, filters={"owner_sub": "someone-else"})
    video_search.search(OWNER, q="豪雨")
    assert env["binds"]["owner"] == OWNER
    assert "va.owner_sub = :owner" in env["sql"]


# --- ルート -------------------------------------------------------------------


@pytest.fixture()
def routed(env):
    app.dependency_overrides[require_video] = lambda: None
    yield env
    app.dependency_overrides.pop(require_video, None)


def test_route_returns_scene_hits_with_reasons(routed):
    res = client.post("/api/video/search", json={
        "q": "豪雨", "filters": {"indoor": "outdoor"}, "limit": 5,
    })
    assert res.status_code == 200
    body = res.json()
    assert body["mode"] == "vector"
    assert body["hits"][0]["matched"]["reason"]
    assert routed["binds"]["lim"] == 5


def test_route_empty_body_is_a_plain_listing(routed):
    res = client.post("/api/video/search", json={})
    assert res.status_code == 200
    assert res.json()["mode"] == "filter"


def test_route_blank_filter_values_are_treated_as_unset(routed):
    """画面のフォームは未入力の欄を空文字で送る。それで 422 にしない。"""
    res = client.post("/api/video/search", json={
        "q": "豪雨", "filters": {"collection": "", "place": "  ", "category": None},
    })
    assert res.status_code == 200
    assert "flt_collection" not in routed["binds"]


def test_route_rejects_unknown_filter_keys(routed):
    res = client.post("/api/video/search", json={"filters": {"collectoin": "x"}})
    assert res.status_code == 422


def test_route_rejects_unknown_top_level_keys(routed):
    """`{"query": "豪雨"}` を無視すると、検索したつもりで全場面を見ることになる。"""
    res = client.post("/api/video/search", json={"query": "豪雨"})
    assert res.status_code == 422
    assert client.post(
        "/api/video/search", json={"similar_scene_id": "s1"}
    ).status_code == 422


def test_route_rejects_bad_enum_and_limit(routed):
    assert client.post(
        "/api/video/search", json={"filters": {"indoor": "outside"}}
    ).status_code == 422
    assert client.post(
        "/api/video/search", json={"limit": 1000}
    ).status_code == 422


def test_route_missing_similar_scene_is_404(routed):
    res = client.post("/api/video/search", json={"similar_to_scene_id": "nope"})
    assert res.status_code == 404


def test_route_maps_embedding_failures_to_502(routed, monkeypatch):
    """上流の障害は 502。**例外の中身はクライアントへ返さない**。

    OCI SDK の例外文字列には request id や内部エンドポイントが載る。
    そのまま detail にすると利用者に見せることになる(原因はログにだけ残す)。
    """
    def boom(*_a, **_kw):
        raise RuntimeError(
            "{'opc-request-id': 'ABC123', 'endpoint': 'https://inference.internal'}"
        )

    monkeypatch.setattr("jetuse_core.embeddings.embed", boom)
    monkeypatch.setattr(video_search, "_query_vector", _REAL_QUERY_VECTOR)
    res = client.post("/api/video/search", json={"q": "豪雨"})
    assert res.status_code == 502
    detail = res.json()["detail"]
    assert "opc-request-id" not in detail and "internal" not in detail
    assert "ベクトルに変換できません" in detail


def test_route_503_when_bucket_not_configured(monkeypatch):
    import service.deps as deps

    monkeypatch.setattr(deps, "get_settings", lambda: SimpleNamespace(video_bucket=""))
    assert client.post("/api/video/search", json={}).status_code == 503
