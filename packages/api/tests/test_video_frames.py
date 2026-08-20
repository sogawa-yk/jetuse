"""VID-02 場面分割とフレーム抽出。

**時刻は ffmpeg の実測から作る**(ADR-0032 決定3)ので、ここで検証するのは
「実測値をどう区間へ畳むか」と「実測できなかったときに黙って 0 件にしないか」の 2 つ。

ffmpeg 本体を呼ぶ層は `_run_ffmpeg` を差し替えて、標準エラーの実物(実機で採取した
書式)をそのまま食わせる。純粋関数(併合・分割・代表フレーム時刻)は ffmpeg 無しで回す。
"""

import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest

from jetuse_core import video_frames as vf

# 実機の ffmpeg 7.1 が出す標準エラー(runs/.../e2e で採取したものを縮めた)。
# 書式が変わればここが落ちる ―― 落ちてくれないと「場面 0 件で正常終了」に化ける。
STDERR_OK = """\
Input #0, mov,mp4,m4a,3gp,3g2,mj2, from '/tmp/x.mp4':
  Metadata:
    encoder         : Lavf61.7.100
  Duration: 00:00:14.90, start: 0.000000, bitrate: 217 kb/s
  Stream #0:0[0x1](und): Video: h264 (High) (avc1 / 0x31637661), yuv420p(progressive), \
320x240 [SAR 1:1 DAR 4:3], 216 kb/s, 10.07 fps, 10 tbr, 1000k tbn (default)
Stream mapping:
  Stream #0:0 -> #0:0 (h264 (native) -> wrapped_avframe (native))
Output #0, null, to 'pipe:':
  Stream #0:0(und): Video: wrapped_avframe, yuv420p(progressive), 320x240, 10 fps
[Parsed_showinfo_1 @ 0x600001608a50] n:   0 pts:5000000 pts_time:5       duration:0 \
fmt:yuv420p s:320x240 i:P iskey:0 type:B
[Parsed_showinfo_1 @ 0x600001608a50] n:   1 pts:10000000 pts_time:10      duration:0 \
fmt:yuv420p s:320x240 i:P iskey:1 type:I
"""

# 転換が 1 つも出ない映像(1 カット)。showinfo 行が無いだけで、Duration は読める。
STDERR_ONE_CUT = """\
Input #0, mov,mp4,m4a,3gp,3g2,mj2, from '/tmp/one.mp4':
  Duration: 00:00:06.00, start: 0.000000, bitrate: 4 kb/s
  Stream #0:0[0x1](und): Video: h264 (High) (avc1 / 0x31637661), yuv420p(progressive), \
320x240 [SAR 1:1 DAR 4:3], 2 kb/s, 10 fps, 10 tbr, 10240 tbn (default)
Output #0, null, to 'pipe:':
  Stream #0:0(und): Video: wrapped_avframe, yuv420p(progressive), 320x240, 10 fps
"""

# 音声のみ。入力に Video: が無く、ffmpeg は非ゼロで終わる。
STDERR_AUDIO_ONLY = """\
Input #0, mov,mp4,m4a,3gp,3g2,mj2, from '/tmp/a.m4a':
  Duration: 00:00:03.00, start: 0.000000, bitrate: 73 kb/s
  Stream #0:0[0x1](und): Audio: aac (LC) (mp4a / 0x6134706D), 44100 Hz, mono, fltp, 69 kb/s
Output #0, null, to 'pipe:':
[out#0/null @ 0x600002004000] Output file does not contain any stream
Error opening output file -.
Error opening output files: Invalid argument
"""

# 壊れた映像。入力自体が開けない。
STDERR_BROKEN = """\
[mov,mp4,m4a,3gp,3g2,mj2 @ 0x134e07980] moov atom not found
[in#0 @ 0x600003c18400] Error opening input: Invalid data found when processing input
Error opening input file /tmp/broken.mp4.
Error opening input files: Invalid data found when processing input
"""

JPEG = b"\xff\xd8\xff\xe0" + b"jpegbody" + b"\xff\xd9"


def fake_ffmpeg(monkeypatch, *, stdout=b"", stderr="", returncode=0):
    """`_run_ffmpeg` を差し替え、渡された引数列を記録して返す。

    **標準エラーはバイト列で返す**(実物と同じ)。str で返す fake にすると、
    復号漏れを本番だけで踏む。
    """
    calls = []

    def run(args, *, timeout):
        calls.append(list(args))
        return subprocess.CompletedProcess(
            args, returncode, stdout, stderr.encode("utf-8")
        )

    monkeypatch.setattr(vf, "_run_ffmpeg", run)
    monkeypatch.setattr(vf, "ffmpeg_exe", lambda: "/fake/ffmpeg")
    return calls


# --- 併合・分割(純粋関数) -----------------------------------------------------


def test_cuts_become_contiguous_scenes():
    scenes = vf.build_scenes([5_000, 10_000], 14_900)
    assert [(s.start_ms, s.end_ms) for s in scenes] == [
        (0, 5_000), (5_000, 10_000), (10_000, 14_900)
    ]


def test_no_cut_still_makes_one_scene():
    """転換が無い映像(1 カット)でも 1 場面として成立する(完了条件)。"""
    scenes = vf.build_scenes([], 6_000)
    assert [(s.start_ms, s.end_ms) for s in scenes] == [(0, 6_000)]


def test_short_segments_are_merged_forward():
    """MIN 未満の区間は次の境界まで飲み込む。3 つの細切れが 1 場面になる。"""
    cuts = [500, 900, 1_400]  # どれも MIN_SCENE_MS(2000) 未満
    scenes = vf.build_scenes(cuts, 10_000)
    assert [(s.start_ms, s.end_ms) for s in scenes] == [(0, 10_000)]


def test_trailing_short_segment_is_absorbed_into_previous():
    """末尾が MIN 未満なら手前へ吸収する(前へ送れないので後ろから畳む)。"""
    scenes = vf.build_scenes([5_000], 5_500)
    assert [(s.start_ms, s.end_ms) for s in scenes] == [(0, 5_500)]


def test_whole_video_shorter_than_min_is_still_one_scene():
    """尺そのものが MIN 未満でも 0 件にしない。1ms でも 1 場面。"""
    assert [(s.start_ms, s.end_ms) for s in vf.build_scenes([], 1)] == [(0, 1)]


def test_exactly_min_length_segment_is_kept():
    """境界値: ちょうど MIN は「短すぎる」ではない。"""
    scenes = vf.build_scenes([vf.MIN_SCENE_MS], vf.MIN_SCENE_MS * 2)
    assert len(scenes) == 2


def test_one_ms_under_min_is_merged():
    scenes = vf.build_scenes([vf.MIN_SCENE_MS - 1], vf.MIN_SCENE_MS * 2)
    assert len(scenes) == 1


def test_long_segment_is_split_evenly():
    """MAX 超えは等分。端数は 1ms 単位で吸収し、隙間も重なりも作らない。"""
    total = vf.MAX_SCENE_MS * 2 + 1_001
    scenes = vf.build_scenes([], total)
    assert len(scenes) == 3
    assert all(s.end_ms - s.start_ms <= vf.MAX_SCENE_MS for s in scenes)
    assert scenes[0].start_ms == 0 and scenes[-1].end_ms == total
    for a, b in zip(scenes, scenes[1:], strict=False):
        assert a.end_ms == b.start_ms


def test_exactly_max_length_segment_is_not_split():
    scenes = vf.build_scenes([], vf.MAX_SCENE_MS)
    assert len(scenes) == 1


def test_one_ms_over_max_is_split():
    scenes = vf.build_scenes([], vf.MAX_SCENE_MS + 1)
    assert len(scenes) == 2


def test_cuts_outside_the_video_are_ignored():
    """0 以下 / 尺以上 / 重複した転換時刻は境界にしない(ゼロ長の場面を作らない)。"""
    scenes = vf.build_scenes([0, -10, 5_000, 5_000, 10_000, 99_000], 10_000)
    assert [(s.start_ms, s.end_ms) for s in scenes] == [(0, 5_000), (5_000, 10_000)]


def test_unsorted_cuts_are_sorted():
    scenes = vf.build_scenes([10_000, 5_000], 14_000)
    assert [s.start_ms for s in scenes] == [0, 5_000, 10_000]


@pytest.mark.parametrize("bad", [0, -1])
def test_zero_or_negative_duration_is_an_error(bad):
    """尺が測れない映像を「場面 0 件」で正常扱いにしない。"""
    with pytest.raises(vf.VideoDecodeError):
        vf.build_scenes([], bad)


def test_every_scene_satisfies_the_db_constraint():
    """024 の CHECK (start_ms >= 0 AND end_ms > start_ms) を必ず満たす。

    ここが崩れると INSERT が落ちる ―― DB の制約と生成側の不変条件を一致させる。
    """
    scenes = vf.build_scenes([1, 2, 3, 7_000, 80_000], 90_000)
    assert scenes
    for s in scenes:
        assert s.start_ms >= 0
        assert s.end_ms > s.start_ms


# --- 代表フレームの時刻 -------------------------------------------------------


def test_frame_times_are_inside_the_scene():
    times = vf.frame_times(vf.Scene(10_000, 20_000))
    assert times == [12_500, 15_000, 17_500]
    assert all(10_000 < t < 20_000 for t in times)


def test_short_scene_gets_fewer_frames():
    """1 秒に満たない幅から 3 枚取っても同じ絵が並ぶだけ。枚数を落とす。"""
    assert len(vf.frame_times(vf.Scene(0, 900))) == 1
    assert len(vf.frame_times(vf.Scene(0, 2_100))) == 2


def test_frame_times_never_pass_max_ms():
    """末尾のフレームより後ろを指すと ffmpeg が 0 バイトを返す(実機で確認)。"""
    times = vf.frame_times(vf.Scene(0, 10_000), max_ms=3_000)
    assert times and max(times) <= 3_000
    assert len(times) == len(set(times))


def test_frame_times_stay_inside_the_scene_even_when_the_margin_is_wider():
    """末尾の余白が区間より広くても、区間の外は指さない。

    低 fps の映像(例: 0.5fps → 余白 4 秒)では起きうる。素直にクランプすると
    `start_ms` より前を指し、**隣の場面の絵をこの場面のサムネイルとして保存する**。
    """
    scene = vf.Scene(9_900, 10_000)
    times = vf.frame_times(scene, max_ms=9_800)
    assert times
    for t in times:
        assert scene.start_ms <= t < scene.end_ms


@pytest.mark.parametrize("max_ms", [0, 1, 9_000, 9_899, 9_900, 9_999])
def test_frame_times_are_always_inside_the_scene(max_ms):
    scene = vf.Scene(9_900, 10_000)
    times = vf.frame_times(scene, max_ms=max_ms)
    assert times
    assert all(scene.start_ms <= t < scene.end_ms for t in times)


# --- 標準エラーの解釈 ---------------------------------------------------------


def test_probe_reads_duration_size_and_fps():
    info = vf.parse_probe(STDERR_OK)
    assert info.duration_ms == 14_900
    assert (info.width, info.height) == (320, 240)
    assert info.fps == pytest.approx(10.07)


def test_cuts_are_read_from_showinfo():
    assert vf.parse_cuts(STDERR_OK) == [5_000, 10_000]


def test_no_showinfo_means_no_cut_not_an_error():
    assert vf.parse_cuts(STDERR_ONE_CUT) == []


def test_one_cut_video_is_probed():
    info = vf.parse_probe(STDERR_ONE_CUT)
    assert info.duration_ms == 6_000
    assert (info.width, info.height) == (320, 240)


def test_output_section_is_not_mistaken_for_the_input_stream():
    """出力側にも `Video:` 行が出る。入力側だけを見ないと解像度と fps を取り違える。

    いまのコマンドはフィルタでスケールしないので両者は一致するが、後でフィルタを
    足したときに黙って出力側の値が VIDEO_ASSETS に入るのを止める番人。
    """
    scaled = STDERR_ONE_CUT.replace(
        "Video: wrapped_avframe, yuv420p(progressive), 320x240, 10 fps",
        "Video: wrapped_avframe, yuv420p(progressive), 1280x720, 30 fps",
    )
    info = vf.parse_probe(scaled)
    assert (info.width, info.height) == (320, 240)
    assert info.fps == pytest.approx(10.0)


def test_audio_only_file_is_rejected_with_a_reason():
    with pytest.raises(vf.VideoDecodeError) as e:
        vf.parse_probe(STDERR_AUDIO_ONLY)
    assert "映像ストリーム" in str(e.value)


def test_missing_duration_is_rejected():
    with pytest.raises(vf.VideoDecodeError):
        vf.parse_probe("Input #0\n  Stream #0:0: Video: h264, 320x240, 10 fps\n")


# --- ffmpeg を呼ぶ層 ----------------------------------------------------------


def test_split_scenes_uses_the_scene_filter_and_returns_measured_scenes(monkeypatch):
    calls = fake_ffmpeg(monkeypatch, stderr=STDERR_OK)
    info, scenes = vf.split_scenes("/tmp/x.mp4")
    assert info.duration_ms == 14_900
    assert [(s.start_ms, s.end_ms) for s in scenes] == [
        (0, 5_000), (5_000, 10_000), (10_000, 14_900)
    ]
    joined = " ".join(calls[0])
    assert f"gt(scene,{vf.SCENE_THRESHOLD})" in joined
    assert "showinfo" in joined


def test_split_scenes_raises_when_ffmpeg_fails(monkeypatch):
    """**握りつぶして 0 件にしない**(tasks/VID-02.md 禁止事項)。"""
    fake_ffmpeg(monkeypatch, stderr=STDERR_BROKEN, returncode=183)
    with pytest.raises(vf.VideoDecodeError) as e:
        vf.split_scenes("/tmp/broken.mp4")
    assert "moov atom not found" in str(e.value)


def test_split_scenes_raises_for_audio_only(monkeypatch):
    fake_ffmpeg(monkeypatch, stderr=STDERR_AUDIO_ONLY, returncode=234)
    with pytest.raises(vf.VideoDecodeError) as e:
        vf.split_scenes("/tmp/a.m4a")
    assert "映像ストリーム" in str(e.value)


def test_missing_ffmpeg_binary_is_reported_as_such(monkeypatch):
    """imageio-ffmpeg は入っているが同梱バイナリを解決できない場合。"""
    import imageio_ffmpeg

    def boom():
        raise RuntimeError("No ffmpeg exe could be found. Install ffmpeg on your system")

    monkeypatch.setattr(imageio_ffmpeg, "get_ffmpeg_exe", boom)
    with pytest.raises(vf.FfmpegUnavailableError) as e:
        vf.ffmpeg_exe()
    assert "imageio-ffmpeg" in str(e.value)


def test_missing_imageio_ffmpeg_package_is_reported_as_such(monkeypatch):
    """依存が入っていない環境でも `ImportError` のまま外へ出さない。"""
    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", None)
    with pytest.raises(vf.FfmpegUnavailableError):
        vf.ffmpeg_exe()


def test_unavailable_ffmpeg_stops_split_scenes(monkeypatch):
    def boom():
        raise vf.FfmpegUnavailableError("no ffmpeg")

    monkeypatch.setattr(vf, "ffmpeg_exe", boom)
    with pytest.raises(vf.FfmpegUnavailableError):
        vf.split_scenes("/tmp/x.mp4")


def test_ffmpeg_not_on_disk_is_reported_as_unavailable(monkeypatch):
    """exe のパスは取れたのに起動できない場合も「無い」として扱う。"""
    monkeypatch.setattr(vf, "ffmpeg_exe", lambda: "/fake/ffmpeg")

    def run(args, *, timeout):
        raise FileNotFoundError(args[0])

    monkeypatch.setattr(vf, "_run_ffmpeg", run)
    with pytest.raises(vf.FfmpegUnavailableError):
        vf.split_scenes("/tmp/x.mp4")


def test_ffmpeg_timeout_is_reported(monkeypatch):
    monkeypatch.setattr(vf, "ffmpeg_exe", lambda: "/fake/ffmpeg")

    def run(args, *, timeout):
        raise subprocess.TimeoutExpired(args, timeout)

    monkeypatch.setattr(vf, "_run_ffmpeg", run)
    with pytest.raises(vf.VideoFrameError) as e:
        vf.split_scenes("/tmp/x.mp4")
    assert "timed out" in str(e.value).lower()


def test_extract_frame_returns_jpeg_bytes(monkeypatch):
    calls = fake_ffmpeg(monkeypatch, stdout=JPEG)
    data = vf.extract_frame("/tmp/x.mp4", 2_500, width=320)
    assert data == JPEG
    joined = " ".join(calls[0])
    assert "-ss 2.500" in joined and "mjpeg" in joined


def test_extract_frame_rejects_empty_output(monkeypatch):
    """尺を越えた位置を指すと ffmpeg は成功のまま 0 バイトを返す(実機で確認)。
    0 バイトのサムネイルを Object Storage へ置かない。
    """
    fake_ffmpeg(monkeypatch, stdout=b"")
    with pytest.raises(vf.VideoDecodeError):
        vf.extract_frame("/tmp/x.mp4", 99_000, width=320)


def test_extract_frame_rejects_non_jpeg_output(monkeypatch):
    fake_ffmpeg(monkeypatch, stdout=b"not a jpeg at all")
    with pytest.raises(vf.VideoDecodeError):
        vf.extract_frame("/tmp/x.mp4", 1_000, width=320)


def test_scene_frames_extracts_several(monkeypatch):
    calls = fake_ffmpeg(monkeypatch, stdout=JPEG)
    frames = vf.scene_frames("/tmp/x.mp4", vf.Scene(0, 10_000))
    assert len(frames) == vf.FRAMES_PER_SCENE
    assert len(calls) == vf.FRAMES_PER_SCENE


# --- Object Storage と台帳へ書くところ ----------------------------------------


class FakeRaw:
    def __init__(self, data: bytes):
        self.data = data

    def stream(self, amt, decode_content=False):
        for i in range(0, len(self.data), amt):
            yield self.data[i:i + amt]


class FakeBody:
    def __init__(self, data: bytes):
        self.raw = FakeRaw(data)


class Resp:
    def __init__(self, data):
        self.data = data


class FakeObject:
    def __init__(self, name, time_created):
        self.name = name
        self.time_created = time_created


class FakeListObjects:
    def __init__(self, objects, next_start_with=None):
        self.objects = objects
        self.next_start_with = next_start_with


class FakeOS:
    """put/delete/list だけの最小 Object Storage。**作成時刻も持つ** ——
    掃除の線引きが経過時間で決まるので、時刻の無い fake では規則を検証できない。
    """

    def __init__(self, source: bytes = b"fake-mp4-bytes"):
        self.source = source
        self.objects: dict[str, bytes] = {}
        self.created: dict[str, datetime] = {}
        self.content_types: dict[str, str] = {}
        self.deleted: list[str] = []
        self.fail_delete: set[str] = set()
        self.now = datetime.now(UTC)

    def get_namespace(self):
        return Resp("ns")

    def get_object(self, ns, bucket, name):
        return Resp(FakeBody(self.source))

    def put_object(self, ns, bucket, name, body, **kw):
        self.objects[name] = body
        self.created.setdefault(name, self.now)
        self.content_types[name] = kw.get("content_type", "")
        return Resp(None)

    def seed(self, name: str, body: bytes, age_seconds: float) -> None:
        """既に在ったオブジェクトを、指定した古さで置く。"""
        self.objects[name] = body
        self.created[name] = self.now - timedelta(seconds=age_seconds)

    def delete_object(self, ns, bucket, name):
        if name in self.fail_delete:
            raise RuntimeError(f"503 from Object Storage: {name}")
        if name not in self.objects:
            raise RuntimeError(f"404 ObjectNotFound: {name}")
        self.objects.pop(name, None)
        self.created.pop(name, None)
        self.deleted.append(name)
        return Resp(None)

    def list_objects(self, ns, bucket, prefix=None, fields=None, start=None, **kw):
        names = sorted(n for n in self.objects if n.startswith(prefix or ""))
        if start:
            names = [n for n in names if n >= start]
        objs = [FakeObject(n, self.created[n]) for n in names]
        # 1 ページ 1 件に刻み、全ページ辿らない実装を落とす
        if len(objs) > 1:
            return Resp(FakeListObjects(objs[:1], next_start_with=names[1]))
        return Resp(FakeListObjects(objs))


class FakeVar:
    """`cursor.var()` の代わり。`RETURNING ... INTO` の受け皿。"""

    def __init__(self):
        self._value = None

    def set(self, value):
        self._value = value

    def getvalue(self):
        return self._value


class FakeCursor:
    """`video_frames` と `video.claim_analysis` / `finish_analysis` が投げる SQL だけを解釈する。

    **想定外の SQL は黙って成功させない**（AssertionError にする）。素通しにすると、
    条件を落とした UPDATE がテストを通ってしまう。
    """

    def __init__(self, db):
        self.db = db
        self.rows: list[tuple] = []
        self.rowcount = 0

    def var(self, _type):
        return FakeVar()

    def execute(self, sql, **binds):
        s = " ".join(sql.split())
        self.rowcount = 0
        key = (binds.get("id"), binds.get("o"))
        if s.endswith("FOR UPDATE") and "analysis_token = :tok" in s:
            # 権利の印の照合（行ロック付き）。汎用の SELECT 1 より前に見る
            asset = self.db["assets"].get(key)
            self.rows = [(1,)] if (
                asset is not None and asset.get("token") == binds["tok"]
            ) else []
        elif s.startswith("SELECT 1 FROM video_assets"):
            self.rows = [(1,)] if key in self.db["assets"] else []
        elif s.startswith("UPDATE video_assets SET analysis_state = 'running'"):
            # 入口のアトミック UPDATE（specs/20 §3）。条件を fake でも同じ形で評価する
            asset = self.db["assets"].get(key)
            if asset is not None and (
                asset["state"] != "running"
                or asset["started_at"] is None
                or asset["started_at"] < binds["stale"]
            ):
                asset.update(state="running", started_at=self.db["clock"],
                             token=binds["tok"], error=None)
                self.rowcount = 1
        elif s.startswith("UPDATE video_assets SET analysis_state = :s"):
            asset = self.db["assets"].get(key)
            # token を渡されたら、権利がまだ自分のものである場合だけ書く
            if asset is not None and (
                binds["tok"] is None or asset.get("token") == binds["tok"]
            ):
                asset.update(state=binds["s"], error=binds["e"])
                self.rowcount = 1
        elif s.startswith("DELETE FROM video_scenes"):
            self.db["scenes"] = [
                r for r in self.db["scenes"] if r["asset"] != binds["id"]
            ]
        elif s.startswith("UPDATE video_assets SET duration_ms"):
            self.db["updates"].append(dict(binds))
            self.rowcount = 1
        else:
            raise AssertionError(f"unexpected SQL: {s[:80]}")

    def executemany(self, sql, rows):
        s = " ".join(sql.split())
        assert s.startswith("INSERT INTO video_scenes"), s[:80]
        for r in rows:
            # 024 の CHECK を fake でも強制する。制約違反を単体で落とせるようにする
            if r["s"] < 0 or r["e"] <= r["s"]:
                raise AssertionError(f"video_scenes_span_ck violated: {r}")
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


@pytest.fixture()
def store(monkeypatch):
    import contextlib as _contextlib

    now = datetime.now(UTC).replace(tzinfo=None)
    db = {
        "assets": {("a1", "dev-user"): {"state": "pending", "started_at": None,
                                        "token": None, "error": None}},
        "scenes": [], "updates": [], "now": now, "clock": now,
    }
    os_client = FakeOS()

    @_contextlib.contextmanager
    def fake_connect():
        yield FakeConn(db)

    monkeypatch.setattr(vf, "connect", fake_connect)
    monkeypatch.setattr(vf.video, "connect", fake_connect)
    monkeypatch.setattr(vf.video, "os_client", lambda: os_client)
    monkeypatch.setattr(vf.video, "require_bucket", lambda: "jetuse-loop-video")
    monkeypatch.setattr(
        vf.video, "object_name_for",
        lambda owner, aid: "video/dev-user/a1/source.mp4"
        if (aid, owner) in db["assets"] else None,
    )
    return {"db": db, "os": os_client, "asset": db["assets"][("a1", "dev-user")]}


OLD = "video/dev-user/a1/thumb/oldgen/scene-0000.jpg"
AGED = vf.ORPHAN_GRACE_S + 60      # 回収路が触ってよい古さ
FRESH = vf.ORPHAN_GRACE_S - 60     # 登録の途中かもしれない新しさ


def test_split_asset_scenes_stores_thumbnails_and_duration(monkeypatch, store):
    fake_ffmpeg(monkeypatch, stdout=JPEG, stderr=STDERR_OK)
    result = vf.split_asset_scenes("dev-user", "a1")

    assert result["duration_ms"] == 14_900
    assert len(result["scenes"]) == 3
    names = sorted(store["os"].objects)
    # thumb/<世代>/scene-NNNN.jpg。世代は分析 1 回ぶんの区画
    assert [n.rsplit("/", 1)[1] for n in names] == [
        "scene-0000.jpg", "scene-0001.jpg", "scene-0002.jpg"
    ]
    assert len({n.rsplit("/", 2)[1] for n in names}) == 1
    assert set(store["os"].content_types.values()) == {"image/jpeg"}
    assert [(r["s"], r["e"]) for r in store["db"]["scenes"]] == [
        (0, 5_000), (5_000, 10_000), (10_000, 14_900)
    ]
    assert store["db"]["updates"][-1]["d"] == 14_900
    assert store["db"]["updates"][-1]["t"] == names[0]
    assert [r["thumb"] for r in store["db"]["scenes"]] == names


def test_state_returns_to_pending_after_a_successful_split(monkeypatch, store):
    """割れたのは場面だけ。説明・要約・埋め込みはまだなので `done` にしない。"""
    fake_ffmpeg(monkeypatch, stdout=JPEG, stderr=STDERR_ONE_CUT)
    vf.split_asset_scenes("dev-user", "a1")
    assert store["asset"]["state"] == "pending"
    assert store["asset"]["error"] is None


# --- 同時実行の入口（specs/20 §3） --------------------------------------------


def test_second_analysis_is_rejected_while_one_is_running(monkeypatch, store):
    """1つの映像に対する分析は同時に1つだけ（取れなかった側は 409 相当）。"""
    fake_ffmpeg(monkeypatch, stdout=JPEG, stderr=STDERR_ONE_CUT)
    seen = []

    real_download = vf._download

    def download_then_try_again(object_name):
        # 1 本目が走っている最中に 2 本目を投げる
        if not seen:
            seen.append(1)
            with pytest.raises(vf.video.AnalysisInProgressError):
                vf.split_asset_scenes("dev-user", "a1")
        return real_download(object_name)

    monkeypatch.setattr(vf, "_download", download_then_try_again)
    vf.split_asset_scenes("dev-user", "a1")
    assert seen == [1]           # 2 本目は実際に弾かれた
    assert store["asset"]["state"] == "pending"


def test_a_stale_running_claim_can_be_taken_over(monkeypatch, store):
    """落ちた分析が `running` のまま残っても、再分析できなくならない。"""
    store["asset"].update(
        state="running",
        started_at=store["db"]["now"] - timedelta(
            seconds=vf.video.ANALYSIS_STALE_SECONDS + 60
        ),
    )
    fake_ffmpeg(monkeypatch, stdout=JPEG, stderr=STDERR_ONE_CUT)
    vf.split_asset_scenes("dev-user", "a1")
    assert store["asset"]["state"] == "pending"


def test_a_fresh_running_claim_is_not_taken_over(store):
    store["asset"].update(state="running", started_at=store["db"]["now"])
    with pytest.raises(vf.video.AnalysisInProgressError):
        vf.video.claim_analysis("dev-user", "a1")


def test_claim_on_another_owners_asset_is_not_found(store):
    """他人の映像は「存在しない」扱い（409 と 404 を取り違えさせない）。"""
    with pytest.raises(LookupError):
        vf.video.claim_analysis("someone-else", "a1")


def test_a_superseded_run_does_not_sweep_the_new_generation(monkeypatch, store):
    """**台帳を書いた後に引き継がれても**、新しい実行が置いた世代を消さない。

    掃除の対象を「台帳が指していないもの」にすると、引き継いだ側が置いたばかりの
    （まだ台帳に載っていない）世代まで消してしまい、その実行が台帳を書いた瞬間に
    `thumb_object` が消えたオブジェクトを指す。対象を「自分が置く前から在ったもの」に
    限れば、後から始まった実行の成果を巻き込みようがない。
    """
    store["os"].seed(OLD, b"old", 1)
    fake_ffmpeg(monkeypatch, stdout=JPEG, stderr=STDERR_ONE_CUT)
    real_save = vf._save_scenes
    newcomer = "video/dev-user/a1/thumb/newgen/scene-0000.jpg"

    def save_then_hand_over(owner, aid, token, info, scenes, thumbs):
        real_save(owner, aid, token, info, scenes, thumbs)
        # 保存の直後に別の実行が権利を引き継ぎ、自分の世代を置き始めた
        store["asset"].update(state="running", token="taken-over-by-someone-else")
        store["os"].seed(newcomer, b"new", 0)

    monkeypatch.setattr(vf, "_save_scenes", save_then_hand_over)
    with pytest.raises(vf.video.AnalysisSupersededError):
        vf.split_asset_scenes("dev-user", "a1")

    assert newcomer in store["os"].objects   # 後から来た世代は消さない
    assert OLD in store["os"].deleted        # 自分より前に在ったものは消す


def test_a_superseded_run_writes_nothing(monkeypatch, store):
    """引き継がれた側は、場面も状態も 1 つも書かずに降りる。

    引き継ぎ（stale takeover）は**相手が落ちていることを保証しない**。生きたまま
    引き継がれた古い実行がそのまま書き続けると、新しい実行の場面を上書きし、
    その `running` まで解いて 3 本目の開始を許す ＝「同時に 1 つだけ」が破れる。
    """
    fake_ffmpeg(monkeypatch, stdout=JPEG, stderr=STDERR_ONE_CUT)
    real_download = vf._download

    def download_then_hand_over(object_name):
        # 自分が置いている最中に、別の実行が権利を引き継いだ
        store["asset"].update(state="running", token="taken-over-by-someone-else")
        return real_download(object_name)

    monkeypatch.setattr(vf, "_download", download_then_hand_over)
    with pytest.raises(vf.video.AnalysisSupersededError):
        vf.split_asset_scenes("dev-user", "a1")

    assert store["db"]["scenes"] == []        # 場面を書いていない
    assert store["db"]["updates"] == []       # 尺・サムネイルも書いていない
    assert store["asset"]["state"] == "running"   # 新しい実行の running を解いていない


def test_a_superseded_run_does_not_record_its_failure(monkeypatch, store):
    """引き継がれた側は、自分の失敗で新しい実行を `failed` に落とさない。"""
    fake_ffmpeg(monkeypatch, stderr=STDERR_BROKEN, returncode=183)
    real_download = vf._download

    def download_then_hand_over(object_name):
        store["asset"].update(state="running", token="taken-over-by-someone-else")
        return real_download(object_name)

    monkeypatch.setattr(vf, "_download", download_then_hand_over)
    with pytest.raises(vf.VideoDecodeError):
        vf.split_asset_scenes("dev-user", "a1")

    assert store["asset"]["state"] == "running"
    assert store["asset"]["error"] is None


def test_each_claim_gets_a_distinct_token(store):
    """権利の印は取り直すたびに変わる（同じ値だと引き継ぎを見分けられない）。"""
    first = vf.video.claim_analysis("dev-user", "a1")
    store["asset"]["started_at"] = store["db"]["now"] - timedelta(
        seconds=vf.video.ANALYSIS_STALE_SECONDS + 60
    )
    second = vf.video.claim_analysis("dev-user", "a1")
    assert first != second
    # 古い印では書けない
    assert vf.video.finish_analysis(
        "dev-user", "a1", "pending", None, token=first
    ) is False
    assert vf.video.finish_analysis(
        "dev-user", "a1", "pending", None, token=second
    ) is True


# --- 失敗の記録と後始末 --------------------------------------------------------


def test_failure_records_the_reason_in_the_ledger(monkeypatch, store):
    """**失敗を握りつぶさない**（specs/20 §3）。理由を残して `failed` にする。"""
    fake_ffmpeg(monkeypatch, stderr=STDERR_BROKEN, returncode=183)
    with pytest.raises(vf.VideoDecodeError):
        vf.split_asset_scenes("dev-user", "a1")
    assert store["asset"]["state"] == "failed"
    assert "moov atom not found" in store["asset"]["error"]


def test_failure_keeps_the_previous_thumbnails(monkeypatch, store):
    """途中で落ちても、台帳が指している前回のサムネイルを消さない。

    先に消してから作り直す実装だと、既存の `thumb_object` が消えたオブジェクトを
    指したまま残る（再分析するまで直らない）。
    """
    store["os"].seed(OLD, b"old", 1)
    store["db"]["scenes"].append({"asset": "a1", "s": 0, "e": 1_000, "thumb": OLD})
    fake_ffmpeg(monkeypatch, stderr=STDERR_BROKEN, returncode=183)

    with pytest.raises(vf.VideoDecodeError):
        vf.split_asset_scenes("dev-user", "a1")

    assert store["os"].objects[OLD] == b"old"
    assert store["os"].deleted == []
    assert store["db"]["scenes"] == [{"asset": "a1", "s": 0, "e": 1_000, "thumb": OLD}]


def test_failure_after_upload_deletes_nothing(monkeypatch, store):
    """置いた後に落ちても消さない。置き去りは次に成功した分析が引き取る。"""
    fake_ffmpeg(monkeypatch, stdout=JPEG, stderr=STDERR_OK)

    def boom(*a, **kw):
        raise RuntimeError("ORA-00060: deadlock detected")

    monkeypatch.setattr(vf, "_save_scenes", boom)
    with pytest.raises(RuntimeError):
        vf.split_asset_scenes("dev-user", "a1")

    assert len(store["os"].objects) == 3
    assert store["os"].deleted == []
    assert store["asset"]["state"] == "failed"


def test_reanalysis_replaces_the_generation(monkeypatch, store):
    """成功した分析は、台帳が指していないサムネイルをその場で消す。

    入口が1本（`claim_analysis`）なので、掃除の相手を気にする必要がない。
    """
    store["os"].seed(OLD, b"old", 1)
    fake_ffmpeg(monkeypatch, stdout=JPEG, stderr=STDERR_ONE_CUT)
    vf.split_asset_scenes("dev-user", "a1")

    assert OLD in store["os"].deleted
    assert len(store["os"].objects) == 1
    assert list(store["os"].objects)[0].endswith("/scene-0000.jpg")


def test_orphans_from_a_failed_run_are_cleaned_by_the_next_success(monkeypatch, store):
    orphan = "video/dev-user/a1/thumb/failedgen/scene-0000.jpg"
    store["os"].seed(orphan, b"orphan", 1)
    fake_ffmpeg(monkeypatch, stdout=JPEG, stderr=STDERR_ONE_CUT)
    vf.split_asset_scenes("dev-user", "a1")
    assert orphan in store["os"].deleted


def test_a_failed_delete_does_not_stop_the_rest(monkeypatch, store):
    """掃除の途中で 1 件失敗しても、残りは消す（諦めると消し残しが積む）。"""
    for i in range(3):
        store["os"].seed(f"video/dev-user/a1/thumb/oldgen/scene-000{i}.jpg", b"x", 1)
    fake_ffmpeg(monkeypatch, stdout=JPEG, stderr=STDERR_ONE_CUT)
    store["os"].fail_delete.add("video/dev-user/a1/thumb/oldgen/scene-0001.jpg")
    vf.split_asset_scenes("dev-user", "a1")

    left = sorted(store["os"].objects)
    assert "video/dev-user/a1/thumb/oldgen/scene-0001.jpg" in left
    assert "video/dev-user/a1/thumb/oldgen/scene-0000.jpg" not in left
    assert "video/dev-user/a1/thumb/oldgen/scene-0002.jpg" not in left


def test_cleanup_failure_does_not_undo_a_successful_analysis(monkeypatch, store):
    """掃除に失敗しても、台帳は新しい世代を指したまま成功で返す。"""
    store["os"].seed(OLD, b"old", 1)
    fake_ffmpeg(monkeypatch, stdout=JPEG, stderr=STDERR_ONE_CUT)

    def boom(*a, **kw):
        raise RuntimeError("delete failed")

    monkeypatch.setattr(store["os"], "delete_object", boom)
    result = vf.split_asset_scenes("dev-user", "a1")
    assert len(result["scenes"]) == 1
    assert store["db"]["updates"][-1]["d"] == 6_000
    assert store["asset"]["state"] == "pending"


# --- 台帳に行の無い映像の回収路（specs/20 §3 の範囲外を後から拾う） ---------------


def test_reap_removes_objects_of_assets_with_no_ledger_row(store):
    """分析中に削除された映像の残骸を、後から引き取る。"""
    gone = "video/dev-user/deleted-asset/thumb/g/scene-0000.jpg"
    body = "video/dev-user/deleted-asset/source.mp4"
    store["os"].seed(gone, b"x", AGED)
    store["os"].seed(body, b"y", AGED)

    removed = vf.reap_orphan_assets("dev-user")
    assert sorted(removed) == sorted([body, gone])
    assert gone not in store["os"].objects and body not in store["os"].objects


def test_reap_leaves_assets_that_still_have_a_row(store):
    live = "video/dev-user/a1/thumb/g/scene-0000.jpg"
    store["os"].seed(live, b"x", AGED)
    assert vf.reap_orphan_assets("dev-user") == []
    assert live in store["os"].objects


def test_reap_leaves_recent_objects(store):
    """新しいものには触らない —— 登録の途中（本体を置いて行を入れる前）を巻き込まない。"""
    fresh = "video/dev-user/being-registered/source.mp4"
    store["os"].seed(fresh, b"x", FRESH)
    assert vf.reap_orphan_assets("dev-user") == []
    assert fresh in store["os"].objects


def test_reap_reports_what_it_could_not_delete(store, caplog):
    """消せなかったものは黙って落とさず記録に残す（次の分析がまた拾う）。"""
    gone = "video/dev-user/deleted-asset/source.mp4"
    store["os"].seed(gone, b"x", AGED)
    store["os"].fail_delete.add(gone)

    with caplog.at_level("ERROR"):
        assert vf.reap_orphan_assets("dev-user") == []
    assert gone in store["os"].objects
    assert any("still present after reap" in r.message for r in caplog.records)


def test_reap_runs_before_each_analysis(monkeypatch, store):
    """分析の前段で回収路が走る（削除との競合で残った分を後から拾う）。"""
    gone = "video/dev-user/deleted-asset/source.mp4"
    store["os"].seed(gone, b"x", AGED)
    fake_ffmpeg(monkeypatch, stdout=JPEG, stderr=STDERR_ONE_CUT)
    vf.split_asset_scenes("dev-user", "a1")
    assert gone not in store["os"].objects


# --- 本物の ffmpeg を通す(標準エラーの書式が変わったら落ちる) -------------------
#
# 上のテストは実機で採取した標準エラーを固定文字列で持っている。ffmpeg 側の書式が
# 変わればそれらは**通り続けたまま実機だけ壊れる**ので、少なくとも 1 本は本物を通す。


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    """ffmpeg 自身で検証用の映像を作る(外部ファイルを持ち込まない)。"""
    exe = vf.ffmpeg_exe()
    d = tmp_path_factory.mktemp("clips")

    def run(args):
        subprocess.run([exe, "-y", "-hide_banner", "-loglevel", "error", *args],
                       check=True, capture_output=True, timeout=120)

    three = d / "three.mp4"
    # 5 秒ずつ 3 カット。中身のある画にする(単色は scene スコアが立たない)
    run(["-f", "lavfi", "-i", "testsrc2=s=320x240:d=5:r=10",
         "-f", "lavfi", "-i", "smptebars=s=320x240:d=5:r=10",
         "-f", "lavfi", "-i", "mandelbrot=s=320x240:end_pts=50:rate=10",
         "-filter_complex",
         "[2:v]trim=duration=5,setpts=PTS-STARTPTS[m];[0:v][1:v][m]concat=n=3:v=1:a=0[v]",
         "-map", "[v]", "-pix_fmt", "yuv420p", "-c:v", "libx264", str(three)])

    one = d / "one.mp4"
    run(["-f", "lavfi", "-i", "testsrc2=s=320x240:d=6:r=10",
         "-vf", "hue=s=0", "-pix_fmt", "yuv420p", "-c:v", "libx264", str(one)])

    audio = d / "audio.m4a"
    run(["-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-c:a", "aac", str(audio)])

    broken = d / "broken.mp4"
    broken.write_bytes(three.read_bytes()[:4000])  # moov atom を落とす

    return {"three": three, "one": one, "audio": audio, "broken": broken}


def test_real_ffmpeg_finds_the_cuts(clips):
    info, scenes = vf.split_scenes(clips["three"])
    assert info.width == 320 and info.height == 240
    assert 14_000 <= info.duration_ms <= 15_100
    assert len(scenes) == 3
    # 境界は 5 秒 / 10 秒付近(符号化で 1 フレームずれる)
    assert abs(scenes[0].end_ms - 5_000) <= 200
    assert abs(scenes[1].end_ms - 10_000) <= 200
    assert scenes[-1].end_ms == info.duration_ms


def test_real_ffmpeg_single_cut_video_is_one_scene(clips):
    """転換の無い映像でも 1 場面として成立する(完了条件)。"""
    info, scenes = vf.split_scenes(clips["one"])
    assert len(scenes) == 1
    assert (scenes[0].start_ms, scenes[0].end_ms) == (0, info.duration_ms)


def test_real_ffmpeg_audio_only_fails_with_a_reason(clips):
    with pytest.raises(vf.VideoDecodeError) as e:
        vf.split_scenes(clips["audio"])
    assert "映像ストリーム" in str(e.value)


def test_real_ffmpeg_broken_file_fails_with_a_reason(clips):
    with pytest.raises(vf.VideoDecodeError) as e:
        vf.split_scenes(clips["broken"])
    assert str(e.value).strip()  # 理由なしで落とさない


def test_real_ffmpeg_extracts_representative_frames(clips):
    info, scenes = vf.split_scenes(clips["three"])
    frames = vf.scene_frames(clips["three"], scenes[-1], info=info)
    assert len(frames) == vf.FRAMES_PER_SCENE
    assert all(f.startswith(b"\xff\xd8") and len(f) > 1_000 for f in frames)


def test_real_ffmpeg_frame_at_the_very_end_is_reachable(clips):
    """末尾の場面でも代表フレームが取れる(0 バイトを返させない)。"""
    info, scenes = vf.split_scenes(clips["one"])
    last = vf.frame_times(scenes[-1], max_ms=vf._safe_tail_ms(info))[-1]
    assert vf.extract_frame(clips["one"], last, width=vf.THUMB_WIDTH).startswith(b"\xff\xd8")
