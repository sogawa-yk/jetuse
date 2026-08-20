"""VID-03 場面の記述・要約・埋め込み。

視覚 LLM と ffmpeg と Object Storage と ADB は fake に差し替える。ここで確かめるのは
**LLM が期待どおりに答えなかったときに何が残るか**:

  * JSON を返さない / 項目が欠けた → もっともらしい値で埋めず、`unknown` か失敗にする
  * 一部の場面だけ落ちた → `partial` と**理由**が残る(specs/20 §3「握りつぶさない」)
  * 引き継がれた実行 → 1 行も書かない

時刻(区間)は VID-02 が実測から確定させたものを使い、LLM には聞かない(ADR-0032 決定3)。
"""

import contextlib
import json
import subprocess
from datetime import UTC, datetime

import pytest

from jetuse_core import video
from jetuse_core import video_analyze as va
from jetuse_core import video_frames as vf

OWNER = "dev-user"
ASSET = "a1"
OBJECT = f"video/{OWNER}/{ASSET}/source.mp4"

JPEG = b"\xff\xd8\xff\xe0" + b"jpegbody" + b"\xff\xd9"

# 実機の ffmpeg が出す標準エラー(test_video_frames.py と同じ書式)。転換 2 つ = 3 場面。
STDERR_OK = """\
Input #0, mov,mp4,m4a,3gp,3g2,mj2, from '/tmp/x.mp4':
  Duration: 00:00:14.90, start: 0.000000, bitrate: 217 kb/s
  Stream #0:0[0x1](und): Video: h264 (High) (avc1 / 0x31637661), yuv420p(progressive), \
320x240 [SAR 1:1 DAR 4:3], 216 kb/s, 10.07 fps, 10 tbr, 1000k tbn (default)
Output #0, null, to 'pipe:':
  Stream #0:0(und): Video: wrapped_avframe, yuv420p(progressive), 320x240, 10 fps
[Parsed_showinfo_1 @ 0x1] n:0 pts:5000000 pts_time:5 duration:0 fmt:yuv420p s:320x240
[Parsed_showinfo_1 @ 0x1] n:1 pts:10000000 pts_time:10 duration:0 fmt:yuv420p s:320x240
"""

FULL_ANSWER = {
    "description": "雨の降る屋外で、傘を差した人物がカメラに向かって話している。",
    "tags": ["雨", "屋外", "リポート"],
    "objects": ["傘", "マイク"],
    "people": {"present": "yes", "count": 1},
    "place": "大阪",
    "scene_kind": "屋外",
    "indoor": "outdoor",
    "time_of_day": "day",
    "weather": "雨",
    "actions": ["話している"],
    "screen_text": ["大阪", "OSAKA 12:34"],
}


# --- 応答の解釈(純粋関数) -----------------------------------------------------


def test_plain_json_is_parsed():
    assert va.parse_json_object('{"description": "x"}') == {"description": "x"}


def test_code_fenced_json_is_parsed():
    raw = '```json\n{"description": "x"}\n```'
    assert va.parse_json_object(raw) == {"description": "x"}


def test_json_wrapped_in_prose_is_parsed():
    raw = 'はい、こちらが結果です。\n{"description": "x"}\nご確認ください。'
    assert va.parse_json_object(raw) == {"description": "x"}


@pytest.mark.parametrize("raw", ["", None, "   ", "説明はできません", "```\n?\n```"])
def test_a_non_json_answer_is_an_error_not_an_empty_result(raw):
    """**空の結果に化けさせない。** `{}` を返すと全項目 unknown の場面として保存され、
    「LLM が壊れた応答を返した」ことが記録にも画面にも残らない。"""
    with pytest.raises(va.SceneDescribeError):
        va.parse_json_object(raw)


def test_a_json_array_is_rejected():
    with pytest.raises(va.SceneDescribeError):
        va.parse_json_object('[{"description": "x"}]')


# --- 正規化(欠損と unknown) ---------------------------------------------------


def test_a_full_answer_is_kept_as_is():
    meta = va.normalize_scene(dict(FULL_ANSWER))
    assert meta["place"] == "大阪"
    assert meta["indoor"] == "outdoor"
    assert meta["people"] == {"present": "yes", "count": 1}
    # 画面内文字は改行で 1 つの文字列に。**日本語をそのまま持つ**(要求13)
    assert meta["screen_text"] == "大阪\nOSAKA 12:34"


def test_missing_fields_become_unknown_not_plausible_defaults():
    """判らない項目は `unknown`。**もっともらしい値で埋めない**(tasks/VID-03 禁止事項)。"""
    meta = va.normalize_scene({"description": "何かが映っている。"})
    assert meta["place"] == "unknown"
    assert meta["scene_kind"] == "unknown"
    assert meta["weather"] == "unknown"
    assert meta["indoor"] == "unknown"
    assert meta["time_of_day"] == "unknown"
    assert meta["people"] == {"present": "unknown", "count": "unknown"}
    # 配列に「不明」という第3の状態は作らない(タグに unknown が並ぶと検索が濁る)
    assert meta["tags"] == [] and meta["objects"] == [] and meta["actions"] == []
    # 文字が無ければ NULL。空文字は Oracle が NULL にするので同じこと
    assert meta["screen_text"] is None


@pytest.mark.parametrize("value", ["室内", "INDOOR ", "", None, 1, "yes"])
def test_out_of_range_enums_fall_back_to_unknown(value):
    """CHECK 制約(migration 023)の外の値は `unknown` へ倒す。制約違反にすると、
    失敗理由が「LLM が別の語を返した」ではなく「INSERT が落ちた」になる。"""
    meta = va.normalize_scene({"description": "x", "indoor": value})
    assert meta["indoor"] in ("indoor", "unknown")
    if value == "INDOOR ":
        assert meta["indoor"] == "indoor"  # 前後の空白と大小文字だけは吸収する
    else:
        assert meta["indoor"] == "unknown"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ({"present": "no", "count": 0}, {"present": "no", "count": 0}),
        ({"present": "yes", "count": "3"}, {"present": "yes", "count": 3}),
        ({"present": "yes", "count": "たくさん"},
         {"present": "yes", "count": "unknown"}),
        ({"present": "maybe"}, {"present": "unknown", "count": "unknown"}),
        ("ひとり", {"present": "unknown", "count": "unknown"}),
    ],
)
def test_people_keeps_unknown_apart_from_none(raw, expected):
    """`no`(居ない)と `unknown`(判らない)を混ぜない。"""
    assert va.normalize_scene({"description": "x", "people": raw})["people"] == expected


def test_a_missing_description_is_a_failure_not_an_unknown_scene():
    """説明の無い場面を「分析済み」として保存しない —— 取れなかったことが残らない。"""
    with pytest.raises(va.SceneDescribeError):
        va.normalize_scene({"tags": ["雨"], "place": "unknown"})


def test_unreadable_screen_text_is_unknown_but_absent_text_is_null():
    assert va.normalize_scene(
        {"description": "x", "screen_text": ["unknown"]}
    )["screen_text"] == "unknown"
    assert va.normalize_scene(
        {"description": "x", "screen_text": []}
    )["screen_text"] is None


def test_embedding_text_leaves_unknown_out():
    """`unknown` を載せると、判らない場面どうしが互いに近くなって順位が歪む。"""
    text = va.embedding_text(va.normalize_scene({
        "description": "傘を差した人が話している。",
        "tags": ["雨"], "place": "unknown", "weather": "unknown",
        "screen_text": ["大阪"],
    }))
    assert "unknown" not in text
    assert "傘を差した人が話している。" in text and "雨" in text and "大阪" in text


# --- finish_analysis の値検証(VID-02 レビュー指摘の取り込み) -------------------


def test_a_failure_without_a_reason_is_rejected():
    """`failed` は理由を必ず伴う(specs/20 §3)。docstring の約束を実装で守らせる。"""
    for empty in (None, "", "   "):
        with pytest.raises(ValueError, match="requires a reason"):
            video.finish_analysis(OWNER, ASSET, "failed", empty)
    with pytest.raises(ValueError, match="requires a reason"):
        video.finish_analysis(OWNER, ASSET, "partial", None)


def test_an_unknown_state_is_rejected():
    with pytest.raises(ValueError, match="unknown analysis_state"):
        video.finish_analysis(OWNER, ASSET, "finished", None)


def test_a_success_must_not_carry_a_stale_reason():
    with pytest.raises(ValueError, match="must not carry a reason"):
        video.finish_analysis(OWNER, ASSET, "done", "前回の失敗理由")


# --- ffmpeg を起動できない(VID-02 レビュー指摘の取り込み) ---------------------


@pytest.mark.parametrize(
    "error",
    [
        PermissionError(13, "Permission denied"),
        OSError(8, "Exec format error"),
        FileNotFoundError(2, "No such file or directory"),
    ],
)
def test_a_launch_failure_is_reported_as_unavailable_ffmpeg(monkeypatch, error):
    """起動できない失敗は**配備の不備**。映像の問題(VideoDecodeError)と混ぜない。"""
    def run(args, *, timeout):
        raise error

    monkeypatch.setattr(vf, "_run_ffmpeg", run)
    monkeypatch.setattr(vf, "ffmpeg_exe", lambda: "/fake/ffmpeg")
    with pytest.raises(vf.FfmpegUnavailableError) as e:
        vf.extract_frame("/tmp/x.mp4", 0, width=320)
    assert type(error).__name__ in str(e.value)


# --- fake 一式(Object Storage / ADB / 視覚 LLM) -------------------------------


class Resp:
    def __init__(self, data):
        self.data = data


class FakeRaw:
    def __init__(self, data):
        self.data = data

    def stream(self, amt, decode_content=False):
        yield self.data


class FakeObject:
    def __init__(self, name, time_created):
        self.name = name
        self.time_created = time_created


class FakeListObjects:
    def __init__(self, objects):
        self.objects = objects
        self.next_start_with = None


class FakeOS:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.now = datetime.now(UTC)

    def get_namespace(self):
        return Resp("ns")

    def get_object(self, ns, bucket, name):
        return Resp(type("B", (), {"raw": FakeRaw(b"fake-mp4")})())

    def put_object(self, ns, bucket, name, body, **kw):
        self.objects[name] = body
        return Resp(None)

    def delete_object(self, ns, bucket, name):
        self.objects.pop(name, None)
        return Resp(None)

    def list_objects(self, ns, bucket, prefix=None, fields=None, start=None, **kw):
        names = sorted(n for n in self.objects if n.startswith(prefix or ""))
        return Resp(FakeListObjects([FakeObject(n, self.now) for n in names]))


class FakeCursor:
    """VID-02 / VID-03 が投げる SQL だけを解釈する。

    **想定外の SQL は黙って成功させない**(AssertionError)。素通しにすると、
    条件を落とした UPDATE や、書けていない保存がテストを通ってしまう。
    """

    def __init__(self, db):
        self.db = db
        self.rows: list[tuple] = []
        self.rowcount = 0

    def execute(self, sql, **binds):
        s = " ".join(sql.split())
        self.rowcount = 0
        asset = self.db["asset"]
        mine = binds.get("id") == ASSET and binds.get("o") in (OWNER, None)
        if s.endswith("FOR UPDATE") and "analysis_token = :tok" in s:
            self.rows = [(1,)] if (mine and asset["token"] == binds["tok"]) else []
        elif s.startswith("SELECT 1 FROM video_assets"):
            self.rows = [(1,)] if mine else []
        elif s.startswith("UPDATE video_assets SET analysis_state = 'running'"):
            if asset["state"] != "running":
                asset.update(state="running", token=binds["tok"], error=None)
                self.rowcount = 1
        elif s.startswith("UPDATE video_assets SET analysis_state = :s"):
            if binds["tok"] is None or asset["token"] == binds["tok"]:
                asset.update(state=binds["s"], error=binds["e"])
                self.rowcount = 1
        elif s.startswith("UPDATE video_assets SET vision_state = 'skipped'"):
            if asset["token"] == binds["tok"]:
                asset["vision_state"] = "skipped"
                # 同じ 1 文で前回の要約も消す(fake でも実物と同じ形で評価する)
                assert "summary = NULL" in s, s
                asset["summary"] = None
                self.rowcount = 1
        elif s.startswith("UPDATE video_assets SET summary"):
            asset["summary"] = binds["s"]
            self.rowcount = 1
        elif s.startswith("UPDATE video_assets SET duration_ms"):
            asset["duration_ms"] = binds["d"]
            self.rowcount = 1
        elif s.startswith("DELETE FROM video_scenes"):
            self.db["scenes"].clear()
        elif s.startswith("SELECT id, start_ms, end_ms, thumb_object FROM video_scenes"):
            self.rows = [
                (r["id"], r["s"], r["e"], r["thumb"]) for r in self.db["scenes"]
            ]
        elif s.startswith("UPDATE video_scenes SET description"):
            row = next(r for r in self.db["scenes"] if r["id"] == binds["id"])
            # CHECK 制約(migration 023)を fake でも強制する
            assert binds["indoor"] in ("indoor", "outdoor", "unknown"), binds["indoor"]
            assert binds["tod"] in ("day", "night", "unknown"), binds["tod"]
            for col in ("tags", "objects", "people", "actions"):
                json.loads(binds[col])  # IS JSON 制約
            row.update(binds)
            self.rowcount = 1
        else:
            raise AssertionError(f"unexpected SQL: {s[:90]}")

    def executemany(self, sql, rows):
        assert " ".join(sql.split()).startswith("INSERT INTO video_scenes"), sql
        self.db["scenes"].extend(dict(r) for r in rows)

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
        pass


class FakeLLM:
    """`chat.completions.create` だけを持つ最小のクライアント。

    `answers` は 1 呼び出しにつき 1 つ取り出す。文字列ならそのまま応答本文、
    例外なら送出する(呼び出しが落ちる場合の再現)。使い切ったら最後の値を繰り返す。
    """

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls: list[dict] = []

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, *, model, messages, **kw):
        self.calls.append({"model": model, "messages": messages})
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        message = type("M", (), {"content": answer})()
        return type("R", (), {"choices": [type("C", (), {"message": message})()]})()


@pytest.fixture()
def env(monkeypatch):
    db = {
        "asset": {"state": "pending", "token": None, "error": None,
                  "vision_state": None, "summary": None, "duration_ms": None},
        "scenes": [],
    }
    os_client = FakeOS()

    @contextlib.contextmanager
    def fake_connect():
        yield FakeConn(db)

    for module in (video, vf, va):
        monkeypatch.setattr(module, "connect", fake_connect, raising=False)
    monkeypatch.setattr(video, "os_client", lambda: os_client)
    monkeypatch.setattr(video, "require_bucket", lambda: "jetuse-loop-video")
    monkeypatch.setattr(
        video, "object_name_for",
        lambda owner, aid: OBJECT if (owner, aid) == (OWNER, ASSET) else None,
    )
    # ffmpeg: 場面転換 2 つ(= 3 場面)を返し、フレーム抽出は JPEG を返す
    monkeypatch.setattr(vf, "ffmpeg_exe", lambda: "/fake/ffmpeg")
    monkeypatch.setattr(
        vf, "_run_ffmpeg",
        lambda args, *, timeout: subprocess.CompletedProcess(
            args, 0, JPEG, STDERR_OK.encode()
        ),
    )
    # 埋め込み: 呼ばれた件数ぶんの 1024 次元を返す
    embed_calls: list[list[str]] = []

    def fake_embed(texts, **kw):
        embed_calls.append(list(texts))
        return [[0.01] * 1024 for _ in texts]

    monkeypatch.setattr("jetuse_core.embeddings.embed", fake_embed)
    return {"db": db, "os": os_client, "embed_calls": embed_calls}


def use_llm(monkeypatch, answers) -> FakeLLM:
    llm = FakeLLM(answers)
    monkeypatch.setattr(va, "_client", lambda timeout=180.0: llm)
    return llm


def answer(**overrides) -> str:
    return json.dumps({**FULL_ANSWER, **overrides}, ensure_ascii=False)


# --- 分析の本体(状態遷移) -----------------------------------------------------


def test_a_full_run_describes_every_scene_and_ends_done(env, monkeypatch):
    llm = use_llm(monkeypatch, [answer(), answer(), answer(), "映像全体の要約です。"])
    result = va.analyze_asset(OWNER, ASSET)

    assert result["analysis_state"] == "done"
    assert result["analysis_error"] is None
    assert env["db"]["asset"]["state"] == "done"
    assert env["db"]["asset"]["error"] is None
    # 3 場面 + 要約 = 4 回。**時刻は渡していない**(ADR-0032 決定3)
    assert len(llm.calls) == 4
    assert result["described_count"] == 3
    for row in env["db"]["scenes"]:
        assert row["d"] == FULL_ANSWER["description"]
        assert json.loads(row["tags"]) == FULL_ANSWER["tags"]
        assert json.loads(row["objects"]) == FULL_ANSWER["objects"]
        assert row["text"] == "大阪\nOSAKA 12:34"
        assert row["emb"] is not None
    assert env["db"]["asset"]["summary"] == "映像全体の要約です。"


def test_the_model_is_given_images_only_and_never_the_timestamps(env, monkeypatch):
    """**渡していない情報を答えさせない**(ADR-0032 決定1)。区間は ffmpeg 側が持つ。"""
    llm = use_llm(monkeypatch, [answer(), answer(), answer(), "要約。"])
    va.analyze_asset(OWNER, ASSET)

    content = llm.calls[0]["messages"][0]["content"]
    texts = [c["text"] for c in content if c["type"] == "text"]
    assert len(texts) == 1
    assert "ms" not in texts[0] and "秒" not in texts[0].replace("経過秒数", "")
    assert [c["type"] for c in content].count("image_url") >= 1
    assert all("5000" not in t and "start" not in t for t in texts)


def test_vision_state_is_always_skipped(env, monkeypatch):
    """AI Vision は使わない(実測で不採用)。**通っていないことを台帳に残す**。"""
    use_llm(monkeypatch, [answer(), answer(), answer(), "要約。"])
    result = va.analyze_asset(OWNER, ASSET)
    assert result["vision_state"] == "skipped"
    assert env["db"]["asset"]["vision_state"] == "skipped"


def test_unknown_survives_all_the_way_into_the_row(env, monkeypatch):
    """判らない項目が DB まで `unknown` のまま届く(NULL にも既定値にもしない)。"""
    thin = json.dumps({"description": "何かが映っている。"}, ensure_ascii=False)
    use_llm(monkeypatch, [thin, thin, thin, "要約。"])
    va.analyze_asset(OWNER, ASSET)

    row = env["db"]["scenes"][0]
    assert (row["place"], row["kind"], row["weather"]) == ("unknown",) * 3
    assert (row["indoor"], row["tod"]) == ("unknown", "unknown")
    assert json.loads(row["people"]) == {"present": "unknown", "count": "unknown"}
    assert row["text"] is None  # 文字が無いことは NULL で表す
    assert env["db"]["asset"]["state"] == "done"  # unknown だらけでも失敗ではない


def test_one_broken_answer_makes_the_run_partial_with_a_reason(env, monkeypatch):
    """**一部だけ成功したら `partial`。** 理由を残さない partial は「分析済み」と
    見分けがつかない(specs/20 §3)。"""
    use_llm(monkeypatch, [answer(), "説明できません", answer(), "要約。"])
    result = va.analyze_asset(OWNER, ASSET)

    assert result["analysis_state"] == "partial"
    assert "JSON" in result["analysis_error"]
    assert env["db"]["asset"]["state"] == "partial"
    assert env["db"]["asset"]["error"] == result["analysis_error"]
    # 落ちた 1 場面以外は保存されている
    assert result["described_count"] == 2
    assert sum(1 for r in env["db"]["scenes"] if r.get("d")) == 2


def test_a_missing_description_field_is_counted_as_a_failed_scene(env, monkeypatch):
    """項目が欠けた応答。**残りの項目だけで「分析済み」にしない**。"""
    use_llm(monkeypatch, [json.dumps({"tags": ["雨"]}), answer(), answer(), "要約。"])
    result = va.analyze_asset(OWNER, ASSET)
    assert result["analysis_state"] == "partial"
    assert "description" in result["analysis_error"]


def test_every_scene_failing_ends_failed_with_a_reason(env, monkeypatch):
    use_llm(monkeypatch, ["だめでした"])
    with pytest.raises(va.VideoAnalyzeError):
        va.analyze_asset(OWNER, ASSET)

    assert env["db"]["asset"]["state"] == "failed"
    # **理由の無い失敗を作らない**(VID-02 レビュー指摘)
    assert env["db"]["asset"]["error"]
    assert "記述できませんでした" in env["db"]["asset"]["error"]


def test_a_failing_summary_only_downgrades_to_partial(env, monkeypatch):
    """要約が落ちても場面の記述は残す。捨てるほうが利用者の損。"""
    use_llm(monkeypatch, [answer(), answer(), answer(), RuntimeError("429")])
    result = va.analyze_asset(OWNER, ASSET)
    assert result["analysis_state"] == "partial"
    assert "要約" in result["analysis_error"]
    assert env["db"]["asset"]["summary"] is None
    assert all(r.get("d") for r in env["db"]["scenes"])


def test_a_failing_embedding_keeps_the_descriptions(env, monkeypatch, ):
    use_llm(monkeypatch, [answer(), answer(), answer(), "要約。"])

    def boom(texts, **kw):
        raise RuntimeError("embed 503")

    monkeypatch.setattr("jetuse_core.embeddings.embed", boom)
    result = va.analyze_asset(OWNER, ASSET)
    assert result["analysis_state"] == "partial"
    assert "埋め込み" in result["analysis_error"]
    assert all(r.get("d") and r["emb"] is None for r in env["db"]["scenes"])


def test_embedding_text_is_built_from_the_saved_metadata(env, monkeypatch):
    use_llm(monkeypatch, [answer(), answer(), answer(), "要約。"])
    va.analyze_asset(OWNER, ASSET)
    texts = env["embed_calls"][0]
    assert len(texts) == 3
    assert "大阪" in texts[0] and "雨" in texts[0]


# --- 排他(specs/20 §3「同時実行の範囲」) --------------------------------------


def test_a_second_analysis_is_rejected_while_one_is_running(env, monkeypatch):
    env["db"]["asset"].update(state="running", token="other")
    use_llm(monkeypatch, [answer()])
    with pytest.raises(video.AnalysisInProgressError):
        va.analyze_asset(OWNER, ASSET)


def test_a_superseded_run_writes_nothing(env, monkeypatch):
    """権利を引き継がれた側は、場面も状態も書かずに降りる。"""
    use_llm(monkeypatch, [answer(), answer(), answer(), "要約。"])
    real_describe = va.describe_scene

    def steal(frames, *, model):
        # 記述の途中で別の実行が権利を取った状況
        env["db"]["asset"]["token"] = "someone-else"
        return real_describe(frames, model=model)

    monkeypatch.setattr(va, "describe_scene", steal)
    with pytest.raises(video.AnalysisSupersededError):
        va.analyze_asset(OWNER, ASSET)

    assert env["db"]["asset"]["state"] == "running"  # 新しい実行の running を解かない
    assert env["db"]["asset"]["error"] is None
    assert all(not r.get("d") for r in env["db"]["scenes"])


def test_an_unknown_asset_is_a_lookup_error(env, monkeypatch):
    use_llm(monkeypatch, [answer()])
    with pytest.raises(LookupError):
        va.analyze_asset(OWNER, "does-not-exist")


# --- ルート(specs/20 §3 の入口) ----------------------------------------------


@pytest.fixture()
def routed(env):
    """ルート経由。バケット設定済みとして扱う(依存を上書きする)。"""
    from service.deps import require_video
    from service.main import app

    app.dependency_overrides[require_video] = lambda: None
    yield env
    app.dependency_overrides.pop(require_video, None)


def _client():
    from fastapi.testclient import TestClient

    from service.main import app

    return TestClient(app)


def test_route_returns_the_analysis(routed, monkeypatch):
    use_llm(monkeypatch, [answer(), answer(), answer(), "要約。"])
    res = _client().post(f"/api/video/assets/{ASSET}/analyze")
    assert res.status_code == 200
    body = res.json()
    assert body["analysis_state"] == "done"
    assert body["vision_state"] == "skipped"
    assert len(body["scenes"]) == 3


def test_route_answers_409_while_another_analysis_runs(routed, monkeypatch):
    routed["db"]["asset"].update(state="running", token="other")
    use_llm(monkeypatch, [answer()])
    assert _client().post(f"/api/video/assets/{ASSET}/analyze").status_code == 409


def test_route_answers_404_for_an_unknown_asset(routed, monkeypatch):
    use_llm(monkeypatch, [answer()])
    assert _client().post("/api/video/assets/nope/analyze").status_code == 404


def test_route_reports_the_reason_when_nothing_could_be_described(routed, monkeypatch):
    """**失敗理由を画面にも返す。**「失敗した」だけでは利用者が直せない。"""
    use_llm(monkeypatch, ["だめでした"])
    res = _client().post(f"/api/video/assets/{ASSET}/analyze")
    assert res.status_code == 422
    assert "記述できませんでした" in res.json()["detail"]
    assert routed["db"]["asset"]["state"] == "failed"


def test_route_503_when_bucket_not_configured(env, monkeypatch):
    """VIDEO_BUCKET 未設定は 500 ではなく 503(未設定と故障を混ぜない)。

    `require_video` を上書きしない = 実際の依存が効く状態で叩く。
    """
    from jetuse_core.settings import get_settings

    monkeypatch.setattr(get_settings(), "video_bucket", "")
    assert _client().post(f"/api/video/assets/{ASSET}/analyze").status_code == 503
    # 依存が外れていれば 200 になることを同じ経路で確かめ、上の 503 が
    # 「たまたま未設定だった」ではないことを示す
    monkeypatch.setattr(get_settings(), "video_bucket", "jetuse-loop-video")
    use_llm(monkeypatch, [answer(), answer(), answer(), "要約。"])
    assert _client().post(f"/api/video/assets/{ASSET}/analyze").status_code == 200


def test_route_answers_502_when_the_vision_service_cannot_be_reached(routed, monkeypatch):
    """上流の障害(認証・429・タイムアウト)は 502。**「入力が悪い」に見せない** ——
    映像を差し替えても直らないものを利用者に直させることになる。"""
    use_llm(monkeypatch, [RuntimeError("429 Too Many Requests")])
    res = _client().post(f"/api/video/assets/{ASSET}/analyze")
    assert res.status_code == 502
    assert "429" in res.json()["detail"]
    # 台帳には失敗と理由が残る(specs/20 §3「握りつぶさない」)
    assert routed["db"]["asset"]["state"] == "failed"
    assert "429" in routed["db"]["asset"]["error"]


def test_route_answers_503_when_ffmpeg_cannot_start(routed, monkeypatch):
    """配備の不備は 503。映像の問題(422)と混ぜない。"""
    def boom(args, *, timeout):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(vf, "_run_ffmpeg", boom)
    use_llm(monkeypatch, [answer()])
    res = _client().post(f"/api/video/assets/{ASSET}/analyze")
    assert res.status_code == 503
    assert "PermissionError" in res.json()["detail"]
    assert routed["db"]["asset"]["state"] == "failed"


def test_a_broken_answer_is_not_reported_as_an_upstream_failure(env, monkeypatch):
    """応答の中身の問題は `VisionServiceError` にしない(直し方が違う)。"""
    use_llm(monkeypatch, ["だめでした"])
    with pytest.raises(va.VideoAnalyzeError) as e:
        va.analyze_asset(OWNER, ASSET)
    assert not isinstance(e.value, va.VisionServiceError)


class Truncated:
    """`finish_reason='length'` で返す応答(推論モデルが上限を使い切った状態)。"""

    def __init__(self, content="途中まで書いた要約です。次に、コンピュ"):
        self.content = content

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, *, model, messages, **kw):
        message = type("M", (), {"content": self.content})()
        choice = type("C", (), {"message": message, "finish_reason": "length"})()
        return type("R", (), {"choices": [choice]})()


def test_a_truncated_answer_is_not_stored_as_a_result(monkeypatch):
    """**上限で切れた応答を「分析済み」として保存しない**。

    `gemini-2.5-pro` は思考ぶんも `max_tokens` に数えるので、要約が文の途中で切れる
    ことがある(2026-08-20 実測)。素の文なので JSON の parse では気づけない。
    """
    monkeypatch.setattr(va, "_client", lambda timeout=180.0: Truncated())
    with pytest.raises(va.VideoAnalyzeError, match="切れました"):
        va.summarize(["場面1の説明"])
    with pytest.raises(va.SceneDescribeError, match="切れました"):
        va.describe_scene([JPEG])


def test_a_truncated_summary_only_downgrades_to_partial(env, monkeypatch):
    """要約が切れても場面の記述は残し、`partial` と理由にする。"""
    llm = FakeLLM([answer(), answer(), answer(), "要約。"])
    truncated = Truncated()

    def pick(timeout=180.0):
        # 記述(180s)は正常、要約(120s)だけ上限で切れた状況
        return llm if timeout != 120.0 else truncated

    monkeypatch.setattr(va, "_client", pick)
    result = va.analyze_asset(OWNER, ASSET)
    assert result["analysis_state"] == "partial"
    assert "切れました" in result["analysis_error"]
    assert env["db"]["asset"]["summary"] is None
    assert all(r.get("d") for r in env["db"]["scenes"])


def test_a_reanalysis_does_not_leave_the_previous_summary(env, monkeypatch):
    """**前回の要約を今回の結果として見せない。**

    再分析で要約に失敗すると `summary` は書かれない。消しておかないと、
    `analysis_state` が `partial` なのに前回成功時の要約が画面に残る。
    """
    use_llm(monkeypatch, [answer(), answer(), answer(), "1 回目の要約。"])
    va.analyze_asset(OWNER, ASSET)
    assert env["db"]["asset"]["summary"] == "1 回目の要約。"

    llm = FakeLLM([answer(), answer(), answer(), RuntimeError("429")])
    monkeypatch.setattr(va, "_client", lambda timeout=180.0: llm)
    result = va.analyze_asset(OWNER, ASSET)
    assert result["analysis_state"] == "partial"
    assert env["db"]["asset"]["summary"] is None


def test_a_failing_reanalysis_does_not_leave_the_previous_summary(env, monkeypatch):
    """全場面が落ちる再分析。`_save_analysis` まで届かない経路でも古い要約を残さない。"""
    use_llm(monkeypatch, [answer(), answer(), answer(), "1 回目の要約。"])
    va.analyze_asset(OWNER, ASSET)
    assert env["db"]["asset"]["summary"]

    use_llm(monkeypatch, ["だめでした"])
    with pytest.raises(va.VideoAnalyzeError):
        va.analyze_asset(OWNER, ASSET)
    assert env["db"]["asset"]["state"] == "failed"
    assert env["db"]["asset"]["summary"] is None


def test_an_over_long_summary_is_not_cut_in_the_middle(monkeypatch):
    """上限で切れた応答を弾いておきながら、長い応答を自分で切っては同じこと。"""
    use_llm(monkeypatch, ["あ" * (va.SUMMARY_MAX + 1)])
    with pytest.raises(va.VideoAnalyzeError, match="長すぎます"):
        va.summarize(["場面1の説明"])
    # 上限ちょうどは通る(境界を 1 文字ずらして落とさない)
    use_llm(monkeypatch, ["い" * va.SUMMARY_MAX])
    assert len(va.summarize(["場面1の説明"])) == va.SUMMARY_MAX


def test_more_scenes_than_the_cap_are_reported_not_silently_dropped(env, monkeypatch):
    """**黙って打ち切らない。** 件数の意味も混ぜない(総数と記述数を分ける)。"""
    monkeypatch.setattr(va, "MAX_SCENES", 2)
    use_llm(monkeypatch, [answer(), answer(), "要約。"])
    result = va.analyze_asset(OWNER, ASSET)

    assert result["analysis_state"] == "partial"
    assert "3 件" in result["analysis_error"] and "2 件" in result["analysis_error"]
    # 総数は映像に在る 3 件。記述できたのは 2 件。切り詰めた数も返す
    assert result["scene_count"] == 3
    assert result["described_count"] == 2
    assert result["truncated_scene_count"] == 1
    # 応答には記述できなかった場面も並ぶ(described=False として見える)
    assert [s["described"] for s in result["scenes"]] == [True, True, False]
    assert sum(1 for r in env["db"]["scenes"] if r.get("d")) == 2


class Malformed:
    """形の違う応答（choices が空 / message 無し / 本文が文字列でない）を返す。"""

    def __init__(self, resp):
        self.resp = resp

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, *, model, messages, **kw):
        return self.resp


def _resp(choices):
    return type("R", (), {"choices": choices})()


def _choice(**attrs):
    return type("C", (), {"finish_reason": "stop", **attrs})()


MALFORMED = [
    _resp([]),                                                   # choices が空
    _resp([_choice(message=None)]),                              # message が無い
    _resp([_choice(message=type("M", (), {"content": None})())]),  # 本文が None
    _resp([_choice(message=type("M", (), {"content": {"a": 1}})())]),  # 本文が dict
    type("R", (), {})(),                                         # choices 属性が無い
]


@pytest.mark.parametrize("resp", MALFORMED)
def test_a_malformed_response_raises_our_own_error(monkeypatch, resp):
    """`IndexError` / `AttributeError` を漏らさない。

    漏らすと、呼び出し側は `VideoAnalyzeError` しか部分失敗として扱わないので、
    **要約の応答が壊れているだけで分析全体が failed になり、成功していた場面の
    記述ごと捨てられる**。
    """
    monkeypatch.setattr(va, "_client", lambda timeout=180.0: Malformed(resp))
    with pytest.raises(va.VideoAnalyzeError):
        va.summarize(["場面1の説明"])
    with pytest.raises(va.SceneDescribeError):
        va.describe_scene([JPEG])


def test_a_malformed_summary_response_only_downgrades_to_partial(env, monkeypatch):
    """要約だけ壊れても、場面の記述は保存して `partial` と理由にする。"""
    llm = FakeLLM([answer(), answer(), answer(), "要約。"])
    broken = Malformed(_resp([]))
    monkeypatch.setattr(
        va, "_client", lambda timeout=180.0: llm if timeout != 120.0 else broken
    )
    result = va.analyze_asset(OWNER, ASSET)
    assert result["analysis_state"] == "partial"
    assert "choices" in result["analysis_error"]
    assert all(r.get("d") for r in env["db"]["scenes"])
    assert env["db"]["asset"]["summary"] is None


def test_a_long_japanese_reason_is_cut_by_bytes_not_characters():
    """**理由が長いせいで理由が残らない**ことを防ぐ。

    `analysis_error VARCHAR2(4000)` は CHAR 指定が無いのでバイトで効く。日本語は
    1 文字 3 バイトなので、文字数で切ると 4000 文字未満でも ORA-12899 になり、
    **最後の `finish_analysis` が落ちて `running` のまま固まる**（理由も残らない）。
    """
    reason = "場面の記述に失敗しました。" * 300  # 3600 文字 = 10800 バイト
    fitted = video._fit_bytes(reason, video.ANALYSIS_ERROR_MAX_BYTES)
    assert len(fitted.encode("utf-8")) <= video.ANALYSIS_ERROR_MAX_BYTES
    assert fitted.endswith("…(以下省略)")  # 黙って切らない
    # 収まる長さは触らない
    assert video._fit_bytes("短い理由", 4000) == "短い理由"
    assert video._fit_bytes(None, 4000) is None


def test_many_failing_scenes_still_record_a_reason(env, monkeypatch):
    """場面が多くて理由が長くなっても、台帳に収まる形で必ず残る。"""
    long_error = "だめでした。" * 400
    llm = FakeLLM([answer(), long_error, long_error, "要約。"])
    monkeypatch.setattr(va, "_client", lambda timeout=180.0: llm)
    result = va.analyze_asset(OWNER, ASSET)

    assert result["analysis_state"] == "partial"
    stored = env["db"]["asset"]["error"]
    assert stored and len(stored.encode("utf-8")) <= video.ANALYSIS_ERROR_MAX_BYTES


# --- 再分析の世代(VID-03 レビューの引き継ぎ) ---------------------------------


def test_a_reanalysis_that_dies_early_leaves_no_scenes_from_the_previous_run(
    env, monkeypatch
):
    """**世代を揃えて置き換える。**

    新しい場面を保存する前(映像の取得・ffmpeg・場面分割)で落ちると、以前は前回の
    description / tags / screen_text / embedding が残ったまま `analysis_state` だけ
    今回のものへ動いた。台帳を読んだ側は「いつの分析結果か」を区別できず、検索は
    消えたはずの前回のベクトルで当て続ける。
    """
    use_llm(monkeypatch, [answer(), answer(), answer(), "1 回目の要約。"])
    va.analyze_asset(OWNER, ASSET)
    assert [r["d"] for r in env["db"]["scenes"]] == [FULL_ANSWER["description"]] * 3

    def boom(*a, **kw):
        raise vf.VideoFrameError("映像を取得できませんでした")

    monkeypatch.setattr(vf, "split_claimed_scenes", boom)
    with pytest.raises(vf.VideoFrameError):
        va.analyze_asset(OWNER, ASSET)

    assert env["db"]["asset"]["state"] == "failed"
    assert env["db"]["asset"]["error"]
    assert env["db"]["scenes"] == []   # 前の世代の記述もベクトルも残さない
    assert env["db"]["asset"]["summary"] is None


def test_a_superseded_run_does_not_wipe_the_scenes_of_the_live_one(env, monkeypatch):
    """引き継がれた側は**何も消さずに降りる**。ここは破壊的なので印を必ず見る。"""
    env["db"]["scenes"].append({"id": "live", "s": 0, "e": 1000, "thumb": "t"})
    env["db"]["asset"].update(state="running", token="live-token")
    with pytest.raises(video.AnalysisSupersededError):
        va._begin_analysis(OWNER, ASSET, "stale-token")
    assert [r["id"] for r in env["db"]["scenes"]] == ["live"]


def test_a_broken_vector_only_costs_that_scene_its_embedding(env, monkeypatch):
    """VID-03 の引き継ぎ: 埋め込みの**値**が壊れていても記述は保存し `partial` にする。

    非数値・NaN・次元違いをそのまま渡すと `array.array` 変換か DB の UPDATE で素の
    例外になり、「記述は保存して partial」が破れて分析全体が落ちる。
    """
    use_llm(monkeypatch, [answer(), answer(), answer(), "要約。"])
    monkeypatch.setattr(
        "jetuse_core.embeddings.embed",
        lambda texts, **kw: [
            [0.01] * 1024, [float("nan")] + [0.01] * 1023, [0.01] * 3,
        ],
    )
    result = va.analyze_asset(OWNER, ASSET)

    assert result["analysis_state"] == "partial"
    assert "埋め込みの値が使えない場面が 2 件" in result["analysis_error"]
    assert all(r["d"] for r in env["db"]["scenes"])          # 記述は残る
    stored = [r["emb"] for r in env["db"]["scenes"]]
    assert sum(v is not None for v in stored) == 1           # 壊れた 2 件だけ NULL
