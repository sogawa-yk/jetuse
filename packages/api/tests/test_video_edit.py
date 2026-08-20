"""VID-05 場面メタデータの確認・修正・削除(specs/20 §5 / 要求8)。

ADB と埋め込みは fake に差し替える。ここで確かめるのは **AI の結果を確定情報として
扱わない**ための性質:

  * 出所が動くこと、そして**判らなくならない**こと(`human` を `ai_confirmed` にしない)
  * 直したら**埋め込みを作り直す**こと(直したのに検索結果が変わらないのは筋が通らない)
  * 作り直せなかったときに**古いベクトルを残さない**こと
  * 何を誰がいつ直したかが `VIDEO_SCENE_EDITS` に残ること
  * 他人の場面は「無い」(所有者以外に id の存在有無を漏らさない)
"""

import array
import contextlib
import json

import pytest

from jetuse_core import embeddings, video, video_edit

OWNER = "dev-user"
OTHER = "someone-else"
ASSET = "a1"
SCENE = "s1"

# `video.scene_columns()` が並べる順。**ここで固定しておく** —— 列の順が変わると
# `row_to_scene` の読み替えが静かにずれる(place と scene_kind が入れ替わっても動く)。
SCENE_COLUMNS = (
    "id", "start_ms", "end_ms", "description", "tags", "objects", "people", "actions",
    "place", "scene_kind", "indoor", "time_of_day", "weather", "screen_text",
    "thumb_object", "source", "confirmed_at",
)

AI_SCENE = {
    "id": SCENE, "asset_id": ASSET, "start_ms": 0, "end_ms": 5000,
    "description": "画面は真っ黒で何も映っていません。",
    "tags": json.dumps(["暗転"], ensure_ascii=False),
    "objects": json.dumps([], ensure_ascii=False),
    "people": json.dumps({"present": "no", "count": 0}, ensure_ascii=False),
    "actions": json.dumps([], ensure_ascii=False),
    "place": "unknown", "scene_kind": "unknown", "indoor": "unknown",
    "time_of_day": "unknown", "weather": "unknown", "screen_text": None,
    "thumb_object": f"video/{OWNER}/{ASSET}/thumb/gen1-000.jpg",
    "source": "ai", "confirmed_at": None, "embedding": array.array("f", [0.01] * 1024),
}


# --- fake(ADB / Object Storage / 埋め込み) -----------------------------------


class FakeCursor:
    """VID-05 が投げる SQL だけを解釈する。**想定外の SQL は黙って成功させない。**"""

    def __init__(self, db):
        self.db = db
        self.rows: list[tuple] = []
        self.rowcount = 0

    def _visible(self, scene_id, owner):
        scene = self.db["scenes"].get(scene_id)
        if scene is None:
            return None, None
        asset = self.db["assets"][scene["asset_id"]]
        return (scene, asset) if asset["owner"] == owner else (None, None)

    def execute(self, sql, **binds):
        s = " ".join(sql.split())
        self.rowcount = 0
        if s.startswith("SELECT s.id, s.start_ms"):
            # 所有者の強制は SQL(JOIN + owner_sub)側にある
            assert "a.owner_sub = :o" in s, s
            scene, asset = self._visible(binds["id"], binds["o"])
            self.rows = [] if scene is None else [
                tuple(scene[c] for c in SCENE_COLUMNS)
                + (scene["asset_id"], asset["analysis_state"])
            ]
            self.db["locked"] += 1 if scene is not None and "FOR UPDATE" in s else 0
            if "FOR UPDATE" in s:
                # 映像の行まで掴むとデッドロックし得る(掴む順が分析と逆になる)
                assert "FOR UPDATE OF s.description" in s, s
        elif s.startswith("UPDATE video_scenes SET") and "source = 'human'" in s:
            assert "embedding = :emb" in s, s  # 直したら必ず作り直す
            scene = self.db["scenes"].get(binds["id"])
            if scene and scene["asset_id"] == binds["asset"]:
                for field in video_edit.EDITABLE_FIELDS:
                    if field in binds:
                        assert f"{field} = :{field}" in s, s
                        scene[field] = binds[field]
                json.loads(scene["tags"])  # IS JSON 制約(migration 023)
                scene["source"] = "human"
                scene["confirmed_at"] = self.db["now"]()
                scene["embedding"] = binds["emb"]
                self.rowcount = 1
        elif s.startswith("UPDATE video_scenes SET source = :s"):
            assert binds["s"] in ("ai", "human", "ai_confirmed"), binds["s"]
            scene = self.db["scenes"].get(binds["id"])
            if scene and scene["asset_id"] == binds["asset"]:
                scene["source"] = binds["s"]
                scene["confirmed_at"] = self.db["now"]()
                self.rowcount = 1
        elif s.startswith("DELETE FROM video_scenes"):
            scene = self.db["scenes"].get(binds["id"])
            if scene and scene["asset_id"] == binds["asset"]:
                del self.db["scenes"][binds["id"]]
                # FK ON DELETE CASCADE(migration 023)
                self.db["edits"] = [e for e in self.db["edits"] if e["sid"] != binds["id"]]
                self.rowcount = 1
        elif s.startswith("SELECT field, before_value"):
            rows = [e for e in self.db["edits"] if e["sid"] == binds["id"]]
            self.rows = [
                (e["field"], e["before_value"], e["after_value"], e["editor"], e["at"])
                for e in reversed(rows)
            ][: binds["lim"]]
        else:
            raise AssertionError(f"unexpected SQL: {s[:90]}")

    def executemany(self, sql, rows):
        assert " ".join(sql.split()).startswith("INSERT INTO video_scene_edits"), sql
        for row in rows:
            assert row["editor"], "edited_by は NOT NULL(誰が直したかを残す)"
            self.db["edits"].append({**row, "at": self.db["now"]()})

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class FakeConn:
    def __init__(self, db):
        self.db = db

    def cursor(self):
        return FakeCursor(self.db)

    def commit(self):
        self.db["commits"] += 1


class FakeOS:
    def __init__(self, db):
        self.db = db

    def get_namespace(self):
        return type("R", (), {"data": "ns"})()

    def delete_object(self, ns, bucket, name):
        self.db["deleted_objects"].append(name)


@pytest.fixture()
def env(monkeypatch):
    clock = {"n": 0}

    def now():
        clock["n"] += 1
        return f"2026-08-20T09:00:{clock['n']:02d}Z"

    db = {
        "assets": {ASSET: {"owner": OWNER, "analysis_state": "done"}},
        "scenes": {SCENE: dict(AI_SCENE)},
        "edits": [], "deleted_objects": [], "commits": 0, "locked": 0, "now": now,
    }

    @contextlib.contextmanager
    def fake_connect():
        yield FakeConn(db)

    monkeypatch.setattr(video_edit, "connect", fake_connect)
    monkeypatch.setattr(video, "os_client", lambda: FakeOS(db))
    monkeypatch.setattr(video, "require_bucket", lambda: "jetuse-loop-video")

    calls: list[list[str]] = []

    def fake_embed(texts, **kw):
        calls.append(list(texts))
        return [[0.02] * embeddings.EMBED_DIM for _ in texts]

    monkeypatch.setattr(embeddings, "embed", fake_embed)
    return {"db": db, "embed_calls": calls}


def scene_row(db=None, db_dict=None):
    return (db_dict or db)["scenes"][SCENE]


# --- 入力の正規化(純粋関数) ---------------------------------------------------


def test_only_the_fields_the_spec_lists_can_be_edited():
    with pytest.raises(ValueError, match="直せない項目"):
        video_edit.normalize_edits({"indoor": "indoor"})


def test_an_empty_patch_is_rejected():
    with pytest.raises(ValueError, match="変更する項目がありません"):
        video_edit.normalize_edits({})


@pytest.mark.parametrize("value", ["", "   ", None, 12])
def test_a_description_cannot_be_emptied(value):
    """空にできると「人が消した」のか「人が空にした」のか判らなくなる(消すなら DELETE)。"""
    with pytest.raises(ValueError):
        video_edit.normalize_edits({"description": value})


def test_an_over_long_value_is_refused_not_truncated():
    """**人の入力は黙って切らない。** 切ると保存した内容と送った内容が食い違う。"""
    with pytest.raises(ValueError, match="長すぎます"):
        video_edit.normalize_edits({"place": "あ" * 256})


@pytest.mark.parametrize("tags", ["雨", [1], [{"a": 1}]])
def test_tags_must_be_a_list_of_strings(tags):
    with pytest.raises(ValueError):
        video_edit.normalize_edits({"tags": tags})


def test_an_emptied_place_becomes_unknown_but_screen_text_becomes_null():
    """**NULL と `unknown` を混ぜない**(specs/20 §1)。人が空にしたのは「判らない」で
    あって「まだ分析していない」ではない。画面内文字だけは NULL =「文字が無かった」。"""
    assert video_edit.normalize_edits({"place": ""}) == {"place": "unknown"}
    assert video_edit.normalize_edits({"screen_text": ""}) == {"screen_text": None}


# --- 出所の遷移 ---------------------------------------------------------------


def test_editing_makes_the_scene_human(env):
    res = video_edit.patch_scene(OWNER, SCENE, {"description": "雨の中の中継です。"})
    assert res["source"] == "human"
    assert res["description"] == "雨の中の中継です。"
    assert env["db"]["scenes"][SCENE]["source"] == "human"
    assert res["confirmed_at"] is not None


def test_confirming_an_ai_scene_marks_it_ai_confirmed(env):
    res = video_edit.confirm_scene(OWNER, SCENE)
    assert res["source"] == "ai_confirmed"
    assert res["confirmed_at"] is not None
    # 中身は変えない = 埋め込みを作り直す理由が無い
    assert env["embed_calls"] == []


def test_confirming_a_human_scene_does_not_relabel_it_as_ai(env):
    """**出所を上書きして判らなくしない。** 人が書いた文を `ai_confirmed` にすると
    「AI が書いて人が確認した」ことになる(tasks/VID-05.md 禁止事項)。"""
    video_edit.patch_scene(OWNER, SCENE, {"description": "人が書いた説明です。"})
    res = video_edit.confirm_scene(OWNER, SCENE)
    assert res["source"] == "human"


def test_confirming_twice_keeps_the_source(env):
    video_edit.confirm_scene(OWNER, SCENE)
    assert video_edit.confirm_scene(OWNER, SCENE)["source"] == "ai_confirmed"


def test_the_three_sources_are_distinguishable_through_the_api(env):
    """`ai` / `human` / `ai_confirmed` が API から区別できること(完了条件)。"""
    assert video_edit.list_edits(OWNER, SCENE) == []
    assert env["db"]["scenes"][SCENE]["source"] == "ai"
    assert video_edit.confirm_scene(OWNER, SCENE)["source"] == "ai_confirmed"
    assert video_edit.patch_scene(OWNER, SCENE, {"place": "大阪"})["source"] == "human"


# --- 埋め込みの作り直し -------------------------------------------------------


def test_an_edit_rebuilds_the_embedding_from_the_new_text(env):
    """直したらベクトルを作り直す。**古い説明を載せたまま作らない。**"""
    video_edit.patch_scene(
        OWNER, SCENE, {"description": "雨の中でリポーターが話している。", "tags": ["雨"]}
    )
    assert len(env["embed_calls"]) == 1
    text = env["embed_calls"][0][0]
    assert "雨の中でリポーターが話している。" in text
    assert "雨" in text
    assert "画面は真っ黒" not in text  # 直す前の説明を載せない
    assert "unknown" not in text  # `unknown` はベクトルに混ぜない(順位が歪む)
    stored = env["db"]["scenes"][SCENE]["embedding"]
    assert isinstance(stored, array.array) and len(stored) == embeddings.EMBED_DIM
    assert stored[0] == pytest.approx(0.02)


def test_an_unedited_field_still_feeds_the_new_embedding(env):
    """送っていない項目も**同じ規則で**載せる(分析側と作り方を揃える)。"""
    env["db"]["scenes"][SCENE]["screen_text"] = "大阪 12:34"
    video_edit.patch_scene(OWNER, SCENE, {"place": "大阪"})
    text = env["embed_calls"][0][0]
    assert "大阪 12:34" in text and "大阪" in text


def test_a_failing_embedding_keeps_the_edit_and_clears_the_vector(env, monkeypatch):
    """**古いベクトルを残さない。** 残すと直したのに古い説明で当たり続ける。"""
    monkeypatch.setattr(
        embeddings, "embed",
        lambda texts, **kw: (_ for _ in ()).throw(RuntimeError("429 rate limited")),
    )
    res = video_edit.patch_scene(OWNER, SCENE, {"description": "直した説明です。"})
    assert res["embedding_state"] == "failed"
    assert "429" in res["embedding_error"]
    assert res["description"] == "直した説明です。"
    assert env["db"]["scenes"][SCENE]["embedding"] is None


@pytest.mark.parametrize(
    "bad",
    [
        [0.01] * 1023,                       # 次元違い(列は VECTOR(1024))
        [float("nan")] + [0.01] * 1023,      # NaN
        [float("inf")] + [0.01] * 1023,      # Infinity
        ["0.01"] * 1024,                     # 非数値
        "not-a-vector",
    ],
)
def test_a_broken_embedding_response_does_not_break_the_edit(env, monkeypatch, bad):
    """VID-03 の引き継ぎ: 件数だけでなく**値**を検べる。壊れた値でも編集は保存する。"""
    monkeypatch.setattr(embeddings, "embed", lambda texts, **kw: [bad])
    res = video_edit.patch_scene(OWNER, SCENE, {"description": "直した説明です。"})
    assert res["embedding_state"] == "failed"
    assert env["db"]["scenes"][SCENE]["description"] == "直した説明です。"
    assert env["db"]["scenes"][SCENE]["embedding"] is None


def test_a_scene_that_changed_under_us_is_a_conflict(env, monkeypatch):
    """埋め込みを作っている間に中身が変わったら書かない ——
    **直した内容と埋め込みが食い違う状態を作らない。**"""
    def racing_embed(texts, **kw):
        env["db"]["scenes"][SCENE]["description"] = "別の誰かが直した説明。"
        return [[0.02] * embeddings.EMBED_DIM]

    monkeypatch.setattr(embeddings, "embed", racing_embed)
    with pytest.raises(video_edit.SceneChangedError):
        video_edit.patch_scene(OWNER, SCENE, {"place": "大阪"})
    assert env["db"]["scenes"][SCENE]["place"] == "unknown"  # 1 列も書いていない


# --- 履歴(何を誰がいつ) -------------------------------------------------------


def test_an_edit_records_every_changed_field(env):
    video_edit.patch_scene(OWNER, SCENE, {"description": "直した説明。", "tags": ["雨"]})
    edits = video_edit.list_edits(OWNER, SCENE)
    fields = {e["field"] for e in edits}
    assert {"description", "tags", "source"} <= fields
    described = next(e for e in edits if e["field"] == "description")
    assert described["before"] == AI_SCENE["description"]
    assert described["after"] == "直した説明。"
    assert described["edited_by"] == OWNER
    assert described["edited_at"]
    source = next(e for e in edits if e["field"] == "source")
    assert (source["before"], source["after"]) == ("ai", "human")


def test_an_unchanged_field_is_not_recorded(env):
    """変わっていない項目まで残すと、履歴を見ても何が直ったのか判らなくなる。"""
    video_edit.patch_scene(OWNER, SCENE, {"description": AI_SCENE["description"]})
    fields = [e["field"] for e in video_edit.list_edits(OWNER, SCENE)]
    assert "description" not in fields
    assert "source" in fields  # 出所は ai → human に動いている


def test_confirming_records_the_source_transition(env):
    video_edit.confirm_scene(OWNER, SCENE)
    edits = video_edit.list_edits(OWNER, SCENE)
    source = next(e for e in edits if e["field"] == "source")
    assert (source["before"], source["after"]) == ("ai", "ai_confirmed")
    assert source["edited_by"] == OWNER


def test_the_history_is_newest_first(env):
    video_edit.patch_scene(OWNER, SCENE, {"place": "大阪"})
    video_edit.patch_scene(OWNER, SCENE, {"place": "京都"})
    places = [e for e in video_edit.list_edits(OWNER, SCENE) if e["field"] == "place"]
    assert [(e["before"], e["after"]) for e in places] == [
        ("大阪", "京都"), ("unknown", "大阪")
    ]


# --- 権限(他人の場面) ---------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda: video_edit.patch_scene(OTHER, SCENE, {"description": "乗っ取り"}),
        lambda: video_edit.confirm_scene(OTHER, SCENE),
        lambda: video_edit.delete_scene(OTHER, SCENE),
        lambda: video_edit.list_edits(OTHER, SCENE),
    ],
)
def test_another_owners_scene_does_not_exist(env, call):
    """403 ではなく「無い」。所有者以外に id の存在有無を漏らさない。"""
    with pytest.raises(LookupError):
        call()
    assert env["db"]["scenes"][SCENE]["source"] == "ai"
    assert env["db"]["edits"] == []


# --- 分析中 -------------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda: video_edit.patch_scene(OWNER, SCENE, {"place": "大阪"}),
        lambda: video_edit.confirm_scene(OWNER, SCENE),
        lambda: video_edit.delete_scene(OWNER, SCENE),
    ],
)
def test_editing_is_refused_while_the_asset_is_being_analyzed(env, call):
    """再分析は場面を作り直す。受け取って消すより、消えることを先に伝える。"""
    env["db"]["assets"][ASSET]["analysis_state"] = "running"
    with pytest.raises(video.AnalysisInProgressError):
        call()
    assert env["db"]["scenes"][SCENE]["source"] == "ai"


# --- 削除 ---------------------------------------------------------------------


def test_deleting_a_scene_removes_the_row_and_its_thumbnail(env):
    video_edit.patch_scene(OWNER, SCENE, {"place": "大阪"})
    assert video_edit.delete_scene(OWNER, SCENE) is True
    assert SCENE not in env["db"]["scenes"]
    assert env["db"]["deleted_objects"] == [AI_SCENE["thumb_object"]]
    assert env["db"]["edits"] == []  # 履歴も CASCADE で消える(migration 023)


def test_a_failing_thumbnail_cleanup_does_not_block_the_deletion(env, monkeypatch):
    """消せない「不適切なメタデータ」が利用者に見え続けるほうが悪い(残骸は後で片付く)。"""
    class Broken(FakeOS):
        def delete_object(self, ns, bucket, name):
            raise RuntimeError("object storage unavailable")

    monkeypatch.setattr(video, "os_client", lambda: Broken(env["db"]))
    assert video_edit.delete_scene(OWNER, SCENE) is True
    assert SCENE not in env["db"]["scenes"]


# --- ルート(specs/20 §5 の入口) ----------------------------------------------


@pytest.fixture()
def routed(env):
    from service.deps import require_video
    from service.main import app

    app.dependency_overrides[require_video] = lambda: None
    yield env
    app.dependency_overrides.pop(require_video, None)


def _client():
    from fastapi.testclient import TestClient

    from service.main import app

    return TestClient(app)


def test_route_patch_returns_the_updated_scene(routed):
    res = _client().patch(
        f"/api/video/scenes/{SCENE}", json={"description": "直した説明です。"}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["source"] == "human"
    assert body["embedding_state"] == "ok"
    assert body["changed_fields"]


def test_route_patch_rejects_fields_it_cannot_edit(routed):
    """**黙って捨てない。** 捨てると「直したのに変わらない」理由が判らない。"""
    res = _client().patch(f"/api/video/scenes/{SCENE}", json={"indoor": "indoor"})
    assert res.status_code == 422
    assert routed["db"]["scenes"][SCENE]["source"] == "ai"


def test_route_patch_reports_why_a_value_was_refused(routed):
    res = _client().patch(f"/api/video/scenes/{SCENE}", json={"description": "  "})
    assert res.status_code == 422
    assert "description" in res.json()["detail"]


def test_route_patch_answers_404_for_another_owners_scene(routed):
    routed["db"]["assets"][ASSET]["owner"] = OTHER
    res = _client().patch(f"/api/video/scenes/{SCENE}", json={"place": "大阪"})
    assert res.status_code == 404


def test_route_patch_answers_409_while_the_asset_is_analyzed(routed):
    routed["db"]["assets"][ASSET]["analysis_state"] = "running"
    res = _client().patch(f"/api/video/scenes/{SCENE}", json={"place": "大阪"})
    assert res.status_code == 409


def test_route_confirm_and_edits_and_delete(routed):
    client = _client()
    assert client.post(f"/api/video/scenes/{SCENE}/confirm").json()["source"] == (
        "ai_confirmed"
    )
    edits = client.get(f"/api/video/scenes/{SCENE}/edits").json()["edits"]
    assert [e["field"] for e in edits if e["field"] == "source"] == ["source"]
    assert client.delete(f"/api/video/scenes/{SCENE}").json() == {"deleted": True}
    assert client.get(f"/api/video/scenes/{SCENE}/edits").status_code == 404


def test_route_reports_a_failed_embedding_instead_of_failing_the_edit(routed, monkeypatch):
    monkeypatch.setattr(
        embeddings, "embed",
        lambda texts, **kw: (_ for _ in ()).throw(RuntimeError("upstream down")),
    )
    res = _client().patch(f"/api/video/scenes/{SCENE}", json={"place": "大阪"})
    assert res.status_code == 200
    assert res.json()["embedding_state"] == "failed"
    assert "upstream down" in res.json()["embedding_error"]


# --- 埋め込み応答の検証(VID-03 の引き継ぎ) -----------------------------------


def test_a_good_vector_becomes_float32():
    vector = embeddings.as_vector([0.5] * embeddings.EMBED_DIM)
    assert isinstance(vector, array.array) and vector.typecode == "f"
    assert len(vector) == embeddings.EMBED_DIM


@pytest.mark.parametrize(
    "bad,reason",
    [
        ([0.1] * 512, "次元"),
        ([float("nan")] + [0.1] * 1023, "有限"),
        ([float("-inf")] + [0.1] * 1023, "有限"),
        ([None] + [0.1] * 1023, "数値"),
        ([True] + [0.1] * 1023, "数値"),           # bool は int だが値ではない
        ([1e40] + [0.1] * 1023, "float32"),        # 有限だが float32 に入らない
        ([10**1000] + [0.1] * 1023, "float32"),    # float への変換自体が落ちる桁
        ("x" * 1024, "配列"),
        ({"v": 1}, "配列"),
    ],
)
def test_a_broken_vector_is_a_value_error_not_a_raw_exception(bad, reason):
    """素の TypeError / OverflowError を漏らすと、呼び出し側の「埋め込みだけ落ちた」
    扱い(部分成功)が破れて分析・編集の全体が落ちる。"""
    with pytest.raises(ValueError, match=reason):
        embeddings.as_vector(bad)
