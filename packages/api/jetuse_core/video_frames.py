"""映像を場面(時間区間)へ割り、代表フレームとサムネイルを作る(VID-02 / specs/20 §3 1〜2)。

**時刻はここで確定させる。**場面の境界も尺も `ffmpeg` の実測から作り、以降 LLM には
「この区間に何が映っているか」だけを聞く(ADR-0032 決定3)。LLM は渡していない情報を
作文するので、秒数を答えさせた時点でタイムラインと「その時刻から再生」が壊れる。

`ffmpeg` は **pip の `imageio-ffmpeg`** が同梱する静的バイナリを使う(ADR-0032 決定2)。
`apt-get install ffmpeg` にしないのは、`Containerfile` の「変わりにくい層(依存) →
変わりやすい層(アプリ)」というレイヤ分割を崩さないため。

**失敗を握りつぶさない。** 壊れた映像・音声のみのファイル・`ffmpeg` 自体が無い、の
いずれも例外にする。ここで黙って「場面 0 件」を返すと、呼び出し側は分析が成功したと
見なし、利用者には「分析済みだが何も無い映像」として出る(tasks/VID-02.md 禁止事項)。
"""

import contextlib
import logging
import math
import pathlib
import re
import subprocess
import tempfile
import uuid
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from . import video
from .db import connect

logger = logging.getLogger("jetuse.video_frames")


# --- 定数(根拠つき) -----------------------------------------------------------

# 場面転換とみなす `scene` スコア(0..1)。実測では本物のカット変わりが 0.69〜0.71、
# 同一カット内の揺れが 0.05〜0.08 に出た(320x240 / 10fps / 3 カットの合成映像)。
# 0.3 以下まで下げるとカメラの動きや照明変化を拾って場面が細切れになり、0.5 以上に
# すると似た画どうしのカット変わりを逃す。その間で、余裕のある側へ寄せて 0.4。
SCENE_THRESHOLD = 0.4

# これより短い区間は隣へ併合する。2 秒。根拠は用途側:
#  * 検索結果は「その時刻から再生して確かめる」ためのもの(specs/20 §6)で、
#    2 秒に満たない帯はタイムライン上で掴めず、再生しても内容を確認できない
#  * 場面ごとに視覚 LLM 呼び出しとサムネイル 1 枚が要る。カット数に比例して
#    費用と分析時間が伸びるので、確認できない粒度まで割るのは払い損
MIN_SCENE_MS = 2_000

# これより長い区間は等分する。30 秒。根拠:
#  * 場面転換検出は「画が変わったか」しか見ない。定点カメラや長回しでは画が同じまま
#    内容(人が来る・作業が始まる)が変わるが、転換は出ない。区間が長いほど 1 つの
#    説明文が区間全体を代表しなくなり、検索の当たりどころが曖昧になる
#  * 代表フレーム FRAMES_PER_SCENE 枚で「その区間に何があったか」を賄える上限として、
#    10 秒間隔で 3 枚 = 30 秒を採る
MAX_SCENE_MS = 30_000

# 1 区間から取る代表フレームの枚数。1 枚だと転換直後のフェード中のフレームを掴んで
# 区間全体を代表しないことがある。視覚 LLM のトークンは枚数に比例するので 3 枚に留める。
FRAMES_PER_SCENE = 3

# 代表フレームどうしの最小間隔。これを割ると同じ絵が並ぶだけなので枚数を落とす。
FRAME_MIN_SPACING_MS = 1_000

# 末尾の余白。最後のフレームより後ろを指すと ffmpeg は**成功したまま 0 バイト**を返す
# (実機で確認)。fps から求めた 2 フレーム分を空け、fps が読めない映像では 200ms を使う。
FRAME_TAIL_FRAMES = 2
FRAME_TAIL_FALLBACK_MS = 200

# 視覚 LLM へ渡す代表フレームの横幅と、画面に出すサムネイルの横幅。
# 元映像より大きくは引き伸ばさない(`min(w,iw)`)。
FRAME_WIDTH = 768
THUMB_WIDTH = 320
# JPEG 品質(mjpeg の -q:v。2=最良〜31=最低)。4 は文字の潰れない範囲での実用値。
JPEG_QUALITY = 4

# 台帳に行の無い映像の配下を引き取る(reap_orphan_assets)ときの「十分に古い」の線。
# **登録の途中を巻き込まないためだけの余白**。`create_asset` は本体を Object Storage へ
# 置いてから台帳へ行を入れるので、その隙間に走らせると「行が無い＝孤児」と誤判定する。
# 1 時間は、上限 500MB の映像を上げ切るのに要る時間を十分に上回る幅。
ORPHAN_GRACE_S = 3600

# ffmpeg を待つ上限(秒)。転換検出は全フレームを復号するので長め、1 枚抜きは短く。
# **無制限にしない** —— 壊れた 1 本でワーカーが永久に埋まる。
SCAN_TIMEOUT_S = 900
FRAME_TIMEOUT_S = 60


_DURATION_RE = re.compile(r"^\s*Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)", re.M)
_VIDEO_STREAM_RE = re.compile(r"^\s*Stream #\d+:\d+.*?:\s*Video:\s*(.+)$", re.M)
_AUDIO_STREAM_RE = re.compile(r"^\s*Stream #\d+:\d+.*?:\s*Audio:\s*", re.M)
_SIZE_RE = re.compile(r"\b(\d{2,5})x(\d{2,5})\b")
_FPS_RE = re.compile(r"([\d.]+)\s+fps\b")
_PTS_RE = re.compile(r"Parsed_showinfo.*?\bpts_time:([\d.]+)")

# 標準エラーをそのまま例外文へ入れない(数百行になる)。末尾だけを添える。
_STDERR_TAIL_LINES = 6


class VideoFrameError(RuntimeError):
    """場面分割・フレーム抽出の失敗。理由なしでは投げない。"""


class FfmpegUnavailableError(VideoFrameError):
    """ffmpeg が使えない(依存が無い / バイナリを解決できない / 起動できない)。

    映像そのものの問題と分けるのは、**直し方が違う**から。こちらは配備の不備で、
    映像を差し替えても直らない。
    """


class VideoDecodeError(VideoFrameError):
    """映像として読めない(壊れている / 映像ストリームが無い / 尺が測れない)。"""


@dataclass(frozen=True)
class VideoInfo:
    """実測した映像の素性。`duration_ms` が `VIDEO_ASSETS.duration_ms` になる。"""

    duration_ms: int
    width: int
    height: int
    fps: float


@dataclass(frozen=True)
class Scene:
    """場面 = 半開区間 `[start_ms, end_ms)`。

    **`end_ms > start_ms` を常に満たす**(migration 024 の CHECK と同じ不変条件)。
    幅ゼロの場面はタイムライン上で掴めず、その時刻から再生しても何も確認できない。
    """

    start_ms: int
    end_ms: int

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


# --- ffmpeg の起動 ------------------------------------------------------------


def ffmpeg_exe() -> str:
    """同梱の ffmpeg のパス。解決できなければ `FfmpegUnavailableError`。"""
    try:
        import imageio_ffmpeg
    except ImportError as e:  # 依存が入っていない配備
        raise FfmpegUnavailableError(
            "imageio-ffmpeg がインストールされていません(映像の場面分割に必要です)"
        ) from e
    try:
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:  # 同梱バイナリが無い/実行できないプラットフォーム
        raise FfmpegUnavailableError(
            f"imageio-ffmpeg が ffmpeg バイナリを解決できません: {e}"
        ) from e


def _run_ffmpeg(args: Sequence[str], *, timeout: int) -> subprocess.CompletedProcess:
    """ffmpeg を 1 回起動する(テストはここを差し替える)。

    標準出力はバイト列(JPEG)、標準エラーは文字。**`check=False`** で返し、
    終了コードの解釈は呼び出し側が行う —— どの失敗かで例外の種類が変わる。
    """
    # 引数は定数と実測値のみ。shell は通さない(ファイル名は引数として渡す)
    return subprocess.run(
        list(args), capture_output=True, timeout=timeout, check=False
    )


def _invoke(args: Sequence[str], *, timeout: int) -> tuple[bytes, str, int]:
    try:
        proc = _run_ffmpeg(args, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise VideoFrameError(f"ffmpeg timed out after {timeout}s") from e
    except OSError as e:
        # **起動できない失敗は 1 つに寄せる。** `FileNotFoundError`(パスは取れたが実体が
        # 無い)だけを拾うと、`PermissionError`(実行ビットが落ちている / noexec マウント)や
        # `OSError: Exec format error`(別アーキテクチャの wheel)が生のまま呼び出し側へ
        # 抜け、分析の失敗理由が `PermissionError: ...` という素の型名で台帳に残る。
        # どれも**配備の不備**で、映像を差し替えても直らない —— 映像そのものの問題
        # (`VideoDecodeError`)と混ぜない、というこのモジュールの区別に揃える。
        raise FfmpegUnavailableError(
            f"ffmpeg を起動できません: {type(e).__name__}: {e}"
        ) from e
    stderr = (proc.stderr or b"").decode("utf-8", "replace")
    return proc.stdout or b"", stderr, proc.returncode


def _stderr_tail(stderr: str) -> str:
    lines = [ln for ln in stderr.splitlines() if ln.strip()]
    return " / ".join(lines[-_STDERR_TAIL_LINES:])


# --- 標準エラーの解釈(純粋) ---------------------------------------------------


def _input_section(stderr: str) -> str:
    """入力の記述だけを切り出す。

    ffmpeg は出力側にも `Stream ...: Video: ...` を出す。全文から拾うと、
    フィルタで加工した後の解像度・fps を映像の素性として記録してしまう。
    """
    head = stderr.split("\nOutput #", 1)[0]
    return head.split("Input #", 1)[-1] if "Input #" in head else head


def parse_probe(stderr: str) -> VideoInfo:
    """ffmpeg の標準エラーから尺・解像度・fps を読む。読めなければ例外。"""
    section = _input_section(stderr)

    stream = _VIDEO_STREAM_RE.search(section)
    if not stream:
        if _AUDIO_STREAM_RE.search(section):
            raise VideoDecodeError(
                "映像ストリームがありません(音声のみのファイルのようです)"
            )
        raise VideoDecodeError(
            f"映像ストリームを見つけられません: {_stderr_tail(stderr)}"
        )

    m = _DURATION_RE.search(section)
    if not m:
        # N/A や壊れたヘッダ。尺が無いと最後の場面の終端を決められない
        raise VideoDecodeError(f"映像の尺を読み取れません: {_stderr_tail(stderr)}")
    duration_ms = round(
        (int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))) * 1000
    )
    if duration_ms <= 0:
        raise VideoDecodeError("映像の尺が 0 です")

    line = stream.group(1)
    size = _SIZE_RE.search(line)
    fps = _FPS_RE.search(line)
    return VideoInfo(
        duration_ms=duration_ms,
        width=int(size.group(1)) if size else 0,
        height=int(size.group(2)) if size else 0,
        # fps が読めない映像もある(可変フレームレート等)。0.0 は「不明」で、
        # 末尾余白は FRAME_TAIL_FALLBACK_MS に切り替わる
        fps=float(fps.group(1)) if fps else 0.0,
    )


def parse_cuts(stderr: str) -> list[int]:
    """`showinfo` が出した選択フレームの時刻(ms)。

    **1 件も無いのは異常ではない** —— 転換の無い 1 カットの映像では 0 件になり、
    `build_scenes` が全体を 1 場面にする。異常かどうかは終了コードで判断する。
    """
    return [round(float(t) * 1000) for t in _PTS_RE.findall(stderr)]


# --- 区間の組み立て(純粋) -----------------------------------------------------


def build_scenes(cut_ms: Sequence[int], duration_ms: int) -> list[Scene]:
    """転換時刻と尺から、隙間も重なりも無い場面の並びを作る。

    順に (1) 映像の外や重複した転換時刻を捨て、(2) `MIN_SCENE_MS` 未満の区間を
    次の境界まで飲み込み、(3) `MAX_SCENE_MS` 超えを等分する。

    **必ず 1 件以上返す。**転換が無くても、尺が `MIN_SCENE_MS` に満たなくても、
    映像がある限り場面はある。0 件を返してよいのは例外を投げるときだけ。
    """
    if duration_ms <= 0:
        raise VideoDecodeError(f"映像の尺が不正です: {duration_ms}ms")

    boundaries = sorted({c for c in cut_ms if 0 < c < duration_ms})

    # (2) 短い区間を前から畳む。最後は必ず尺で閉じる
    merged: list[list[int]] = []
    start = 0
    for b in [*boundaries, duration_ms]:
        if b - start >= MIN_SCENE_MS or b == duration_ms:
            merged.append([start, b])
            start = b
    # 末尾だけは前へ送れないので、短ければ手前へ吸収する
    if len(merged) >= 2 and merged[-1][1] - merged[-1][0] < MIN_SCENE_MS:
        merged[-2][1] = merged[-1][1]
        merged.pop()

    # (3) 長い区間を等分。n 等分後の 1 区間は MAX/2 より長く、MIN を割らない
    scenes: list[Scene] = []
    for s, e in merged:
        span = e - s
        n = max(1, math.ceil(span / MAX_SCENE_MS))
        prev = s
        for i in range(1, n + 1):
            nxt = e if i == n else s + round(span * i / n)
            if nxt > prev:  # 1ms 未満の端数で幅ゼロを作らない
                scenes.append(Scene(prev, nxt))
                prev = nxt
    return scenes


def frame_times(
    scene: Scene,
    *,
    count: int = FRAMES_PER_SCENE,
    max_ms: int | None = None,
) -> list[int]:
    """区間から代表フレームを取る時刻。区間の内側に等間隔で置く。

    境界そのものを避けるのは、カット直後がフェード・ブレンドの途中で、区間を
    代表しない絵になりやすいため。`max_ms` は「これより後ろにフレームが無い」上限。

    **上限で区間の外へ出さない。** 低 fps の映像では末尾の余白が区間より広くなりうる
    (例: 0.5fps で余白 4 秒 / 末尾の場面 2 秒)。そこで素直にクランプすると
    `start_ms` より前を指し、**隣の場面の絵をこの場面のサムネイルとして保存する**。
    区間を優先し、そこに本当にフレームが無ければ `extract_frame` が理由付きで落ちる
    —— 誤った絵を黙って保存するより、失敗として見えるほうがよい。
    """
    start, end = scene.start_ms, scene.end_ms
    upper = end if max_ms is None else max(min(end, max_ms), start + 1)
    span = upper - start
    n = max(1, min(count, span // FRAME_MIN_SPACING_MS))
    times = [start + round(span * (i + 1) / (n + 1)) for i in range(n)]
    # 丸めで重なった分は落とす。順序は保つ
    return list(dict.fromkeys(times))


def _safe_tail_ms(info: VideoInfo) -> int:
    """末尾のこれより後ろは指さない、という時刻。"""
    frame_ms = (1000 / info.fps) if info.fps > 0 else 0
    margin = max(round(frame_ms * FRAME_TAIL_FRAMES), FRAME_TAIL_FALLBACK_MS)
    return max(0, info.duration_ms - margin)


# --- 実測(ffmpeg を呼ぶ) ------------------------------------------------------


def split_scenes(
    path: str | pathlib.Path, *, threshold: float = SCENE_THRESHOLD
) -> tuple[VideoInfo, list[Scene]]:
    """1 回の復号で尺・解像度・fps と場面転換を同時に測る。

    `-f null -` で捨てながら全フレームを復号し、標準エラーに出る入力ヘッダ
    (尺・解像度・fps)と `showinfo`(選ばれたフレームの時刻)を両方読む。
    2 回に分けると同じ映像を 2 度復号することになる。
    """
    exe = ffmpeg_exe()
    args = [
        exe, "-hide_banner", "-nostdin",
        "-i", str(path),
        # 音声は場面分割に要らない。復号する分だけ遅くなる
        "-an",
        "-filter:v", f"select='gt(scene,{threshold})',showinfo",
        "-f", "null", "-",
    ]
    _, stderr, code = _invoke(args, timeout=SCAN_TIMEOUT_S)
    if code != 0:
        # 音声のみ・壊れている、どちらも非ゼロで終わる。理由を見分けて返す ——
        # 「映像として読めない」と「そもそも映像が入っていない」では利用者の直し方が違う
        section = _input_section(stderr)
        if _AUDIO_STREAM_RE.search(section) and not _VIDEO_STREAM_RE.search(section):
            raise VideoDecodeError(
                "映像ストリームがありません(音声のみのファイルのようです)"
            )
        raise VideoDecodeError(
            f"ffmpeg が映像を処理できませんでした(exit {code}): {_stderr_tail(stderr)}"
        )

    info = parse_probe(stderr)
    return info, build_scenes(parse_cuts(stderr), info.duration_ms)


def extract_frame(path: str | pathlib.Path, at_ms: int, *, width: int) -> bytes:
    """指定時刻の 1 枚を JPEG で取り出す。

    `-ss` を `-i` より前に置く(入力シーク)。後ろに置くと先頭から復号するので、
    場面数だけ呼ぶこの用途では所要時間が桁で変わる。
    """
    exe = ffmpeg_exe()
    args = [
        exe, "-nostdin", "-hide_banner", "-loglevel", "error",
        "-ss", f"{at_ms / 1000:.3f}",
        "-i", str(path),
        "-frames:v", "1",
        # 引き伸ばさない。高さは -2 で偶数に丸めつつ縦横比を保つ
        "-vf", f"scale='min({width},iw)':-2",
        # mjpeg は full-range を要求する。指定しないと "Non full-range YUV is
        # non-standard" で符号化器が開けない映像がある(実機で確認)
        "-pix_fmt", "yuvj420p",
        "-q:v", str(JPEG_QUALITY),
        "-f", "image2", "-c:v", "mjpeg", "-",
    ]
    stdout, stderr, code = _invoke(args, timeout=FRAME_TIMEOUT_S)
    if code != 0:
        raise VideoDecodeError(
            f"{at_ms}ms のフレームを取り出せませんでした(exit {code}): "
            f"{_stderr_tail(stderr)}"
        )
    # 尺を越えた位置では ffmpeg は**成功したまま何も書かない**。0 バイトを
    # サムネイルとして保存すると、壊れた画像が画面に出るだけで原因が分からなくなる
    if not stdout.startswith(b"\xff\xd8"):
        raise VideoDecodeError(
            f"{at_ms}ms のフレームが JPEG になりませんでした({len(stdout)} バイト)"
        )
    return stdout


def scene_frames(
    path: str | pathlib.Path,
    scene: Scene,
    *,
    width: int = FRAME_WIDTH,
    info: VideoInfo | None = None,
) -> list[bytes]:
    """区間の代表フレーム数枚(視覚 LLM へ渡す用 / specs/20 §3-2)。"""
    max_ms = _safe_tail_ms(info) if info else None
    return [extract_frame(path, t, width=width) for t in frame_times(scene, max_ms=max_ms)]


# --- Object Storage と台帳 ----------------------------------------------------


def thumb_prefix(object_name: str, generation: str = "") -> str:
    """サムネイルの置き場所。`generation` を付けると分析 1 回分の区画になる。

    映像本体と同じプレフィックス配下に置く。`video.delete_asset` は
    プレフィックスごと消すので、映像を消せばサムネイルも一緒に消える。
    """
    base = f"{object_name.rsplit('/', 1)[0]}/thumb/"
    return f"{base}{generation}/" if generation else base


def thumb_object_name(object_name: str, generation: str, index: int) -> str:
    return f"{thumb_prefix(object_name, generation)}scene-{index:04d}.jpg"


@contextlib.contextmanager
def open_asset_video(owner: str, asset_id: str) -> Iterator[pathlib.Path]:
    """映像をローカルの一時ファイルへ落として渡す(後続タスクも使う公開の入口)。

    ffmpeg にはシーク可能なファイルが要る(場面転換検出は全体を舐め、フレーム抽出は
    任意の位置へ飛ぶ)。PAR の URL を直接食わせる手もあるが、抽出のたびに
    HTTP レンジ要求が飛び、期限切れが復号の途中で表に出る。
    """
    object_name = video.object_name_for(owner, asset_id)
    if object_name is None:
        raise LookupError(asset_id)
    with _download(object_name) as path:
        yield path


@contextlib.contextmanager
def _download(object_name: str) -> Iterator[pathlib.Path]:
    bucket = video.require_bucket()
    client = video.os_client()
    ns = client.get_namespace().data
    suffix = pathlib.Path(object_name).suffix or ".mp4"

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="jetuse-video-")) / f"source{suffix}"
    try:
        resp = client.get_object(ns, bucket, object_name)
        with tmp.open("wb") as f:
            # **メモリに読み切らない**。上限 500MB の映像がそのままコンテナに載る
            for chunk in resp.data.raw.stream(1024 * 1024, decode_content=False):
                f.write(chunk)
        yield tmp
    finally:
        # 片方が失敗しても、もう片方は片付ける(残すと一時領域が積み上がる)
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            tmp.parent.rmdir()


def _list_thumbs(client, ns: str, bucket: str, object_name: str) -> list[Any]:
    """その映像のサムネイル置き場に在るオブジェクト(name と time_created)を全部。"""
    out, start = [], None
    while True:
        page = client.list_objects(
            ns, bucket, prefix=thumb_prefix(object_name),
            fields="name,timeCreated", start=start,
        ).data
        out += list(page.objects)
        start = getattr(page, "next_start_with", None)
        if not start:
            break
    return out


def _delete_objects(client, ns: str, bucket: str, names: Iterable[str]) -> list[str]:
    """1 つずつ消す。**1 件の失敗で残りを諦めない**。消せなかった名前を返す。

    片付けの途中で 404(既に別の実行が消した)や一時的な失敗が出ても、残りは消せる。
    諦めると消し残しが積み上がるだけで、誰の役にも立たない。
    **黙って落とさない** —— 消せなかったものは呼び出し側へ返し、記録に残す。
    回収は `reap_orphan_assets`(台帳に行の無い映像の配下)が引き受ける。
    """
    failed = []
    for name in names:
        try:
            client.delete_object(ns, bucket, name)
        except Exception:
            logger.exception("video thumbnail delete failed: %s", name)
            failed.append(name)
    return failed


def reap_orphan_assets(owner: str) -> list[str]:
    """**台帳に行の無い映像**の配下を引き取る(specs/20 §3「同時実行の範囲」の回収路)。

    分析の実行中にその映像が削除されると、削除側の掃除の後に置いたサムネイルが残る。
    **その即時回収は v1 の範囲外**と決めた —— 閉じるには Object Storage(トランザクションが
    無い)と DB をまたぐ分散トランザクションか、映像ごとの外部ロックが要る。実害は
    残骸オブジェクトが数個残ることだけで、データは壊れない。
    **握りつぶすのとは違う**: 起きないことにするのではなく、ここで後から回収すると決めた。

    消すのは (a) 台帳に行の無い映像 id の配下で、(b) 作成から `ORPHAN_GRACE_S` 以上
    経ったものだけ。(b) は登録の途中(本体を置いてから行を入れるまで)を巻き込まないため。

    **件数で打ち切らない。** 打ち切ると、辞書順でその先にある孤児へは何度走らせても
    到達しない(先頭が生きている映像で埋まっていれば恒久的に飢える)。一覧は 1 回の
    API 呼び出しで 1000 件ずつ辿れ、この関数は数秒〜数分かかる分析の前段でしか
    走らないので、所有者ぶんを最後まで見るほうが割に合う。
    """
    bucket = video.require_bucket()
    client = video.os_client()
    ns = client.get_namespace().data
    prefix = f"video/{owner}/"
    cutoff = datetime.now(UTC) - timedelta(seconds=ORPHAN_GRACE_S)

    aged: dict[str, list[str]] = {}
    start = None
    while True:
        page = client.list_objects(
            ns, bucket, prefix=prefix, fields="name,timeCreated", start=start
        ).data
        for obj in page.objects:
            created = getattr(obj, "time_created", None)
            if created is None or created > cutoff:
                continue
            parts = obj.name[len(prefix):].split("/", 1)
            if len(parts) == 2 and parts[0]:
                aged.setdefault(parts[0], []).append(obj.name)
        start = getattr(page, "next_start_with", None)
        if not start:
            break

    removed: list[str] = []
    for asset_id, names in aged.items():
        if video.object_name_for(owner, asset_id) is not None:
            continue
        failed = _delete_objects(client, ns, bucket, names)
        removed += [n for n in names if n not in failed]
        if failed:
            # 次の分析でもう一度拾う。何が残っているかは記録に残す
            logger.error("orphan video objects still present after reap: %s", failed)
    if removed:
        logger.info("reaped %d orphan video object(s) for %s", len(removed), owner)
    return removed


def split_asset_scenes(
    owner: str, asset_id: str, *, threshold: float = SCENE_THRESHOLD
) -> dict[str, Any]:
    """映像 1 本の場面を確定して保存する(VID-02 の入口)。

    分析の権利を取る → Object Storage から落とす → 実測して区間を作る → 場面ごとに
    代表フレームを取り、その中央の 1 枚をサムネイルとして置く → `VIDEO_SCENES` を
    作り直し、`VIDEO_ASSETS.duration_ms` と代表サムネイルを埋める。

    **入口は 1 本**(specs/20 §3「同時実行の範囲」)。`video.claim_analysis` の
    アトミックな UPDATE で権利を取り、取れなければ `AnalysisInProgressError`
    (API は 409)。取り残された `running` は引き継げるが、**引き継ぎは相手が落ちて
    いることを保証しない**ので、権利の印(`analysis_started_at`)を持ち回り、台帳への
    書き込みはその印が一致するときだけ通す。引き継がれた側は何も書かずに
    `AnalysisSupersededError` で降りる。結果として、台帳を書けるのは常に 1 つだけ。

    サムネイルは分析 1 回ぶんを世代(`thumb/<generation>/`)に閉じ込め、
    **置く → 台帳を切り替える → 自分が置く前から在った分を消す**の順で入れ替える。
    先に消すと、その後の復号・アップロード・DB 更新のどこかで落ちた瞬間に、台帳の
    `thumb_object` が消えたオブジェクトを指す(再分析するまで直らない)。
    途中で落ちたときは**何も消さない** —— 台帳は前回の世代を指したままで画面は壊れず、
    置き去りは次に成功した分析が引き取る。

    **状態は `pending` に戻して終わる。** ここで割れたのは場面(specs/20 §3 の 1〜2)
    だけで、説明・要約・埋め込み(3〜6)はまだ走っていない。`done` にすると、説明の無い
    場面を「分析済み」として見せることになる。分析全体を束ねる後続タスクは、同じ
    `claim_analysis` を外側で 1 回取って `done` / `partial` を書く。
    """
    token = video.claim_analysis(owner, asset_id)
    try:
        result = split_claimed_scenes(owner, asset_id, token, threshold=threshold)
    except Exception as e:
        # **失敗を握りつぶさない**(specs/20 §3)。理由を台帳へ残してから投げ直す。
        # 権利が引き継がれていれば書けない —— 新しい実行の running を解かない
        if not video.finish_analysis(
            owner, asset_id, "failed", f"{type(e).__name__}: {e}", token=token
        ):
            logger.warning(
                "video analysis was superseded; not recording failure: %s", asset_id
            )
        raise
    if not video.finish_analysis(owner, asset_id, "pending", None, token=token):
        raise video.AnalysisSupersededError(asset_id)
    return result


def split_claimed_scenes(
    owner: str,
    asset_id: str,
    token: str,
    *,
    threshold: float = SCENE_THRESHOLD,
    path: str | pathlib.Path | None = None,
) -> dict[str, Any]:
    """`claim_analysis` を**外側で取った**実行から呼ぶ場面分割(VID-03 の入口)。

    `token` は権利の印。台帳へ書く `_save_scenes` がこれを照合し、引き継がれていれば
    1 行も書かずに `AnalysisSupersededError` を上げる。状態(`done` / `partial` /
    `failed`)を書くのは、権利を取った外側の責任 —— ここでは触らない。

    `path` に落とし済みの映像を渡せる。分析全体(`video_analyze`)は場面ごとの代表
    フレームも要るので、渡さないと**同じ映像を 2 度 Object Storage から落とす**
    (上限 500MB)。渡された一時ファイルの寿命は呼び出し側が持つ。
    """
    object_name = video.object_name_for(owner, asset_id)
    if object_name is None:
        raise LookupError(asset_id)

    bucket = video.require_bucket()
    client = video.os_client()
    ns = client.get_namespace().data
    generation = uuid.uuid4().hex[:12]

    # 分析の実行中に削除された映像の残骸を引き取る(specs/20 §3 の回収路)。
    # 分析そのものは続けるので、失敗しても止めない
    try:
        reap_orphan_assets(owner)
    except Exception:
        logger.exception("orphan video reap failed (ignored)")

    # **掃除の対象は「自分が置く前から在ったもの」だけ**。ここで控える。
    # 「台帳が指していないもの」を対象にすると、権利を引き継いだ別の実行が
    # 置いたばかりの世代（まだ台帳に載っていない）まで消してしまう ——
    # その実行が台帳を書いた瞬間、`thumb_object` は消えたオブジェクトを指す。
    # 自分より前から在ったものなら、後から始まった実行の成果を巻き込みようがない。
    pre_existing = [o.name for o in _list_thumbs(client, ns, bucket, object_name)]

    uploaded: list[str] = []
    # 渡されていれば落とし直さない(nullcontext は片付けもしない = 呼び出し側の持ち物)
    source = contextlib.nullcontext(path) if path is not None else _download(object_name)
    with source as local:
        info, scenes = split_scenes(local, threshold=threshold)
        max_ms = _safe_tail_ms(info)
        for i, scene in enumerate(scenes):
            # 代表フレームの**真ん中の 1 枚**をサムネイルにする。ここで数枚まとめて
            # 取らないのは、視覚 LLM へ渡す残りが要るのは後続タスクで、そのときに
            # 同じ `open_asset_video` / `scene_frames` で取り直せるため
            times = frame_times(scene, max_ms=max_ms)
            jpeg = extract_frame(local, times[len(times) // 2], width=THUMB_WIDTH)
            name = thumb_object_name(object_name, generation, i)
            client.put_object(ns, bucket, name, jpeg, content_type="image/jpeg")
            uploaded.append(name)

    _save_scenes(owner, asset_id, token, info, scenes, uploaded)

    # 台帳が新しい世代を指した後に、前の世代を消す
    try:
        _delete_objects(client, ns, bucket, pre_existing)
    except Exception:
        # 台帳は既に新しい世代を指している。掃除漏れは課金がわずかに残るだけで、
        # 分析結果は壊れていない。**黙って握り潰さず**ログに出して続ける
        logger.exception("stale video thumbnail cleanup failed (ignored)")

    return {
        "asset_id": asset_id,
        "duration_ms": info.duration_ms,
        "width": info.width,
        "height": info.height,
        "fps": info.fps,
        "scenes": [
            {"start_ms": s.start_ms, "end_ms": s.end_ms, "thumb_object": t}
            for s, t in zip(scenes, uploaded, strict=True)
        ],
    }


def _save_scenes(
    owner: str,
    asset_id: str,
    token: str,
    info: VideoInfo,
    scenes: Sequence[Scene],
    thumbs: Sequence[str],
) -> None:
    """場面を入れ替え、尺と代表サムネイルを台帳へ。1 トランザクションで行う。

    再分析は同じ入口なので、古い場面は消してから入れる。途中で切れると
    「場面が消えただけ」の映像が残るため、削除と挿入を分けてコミットしない。

    **権利の印を `FOR UPDATE` で照合してから書く。** 印が合わなければ、この実行は
    別の実行に引き継がれた側なので 1 行も書かずに降りる。行ロックを取るのは、
    照合から書き込みまでの間に引き継ぎが割り込まないようにするため
    (`claim_analysis` の UPDATE はこのトランザクションが終わるまで待たされる)。
    """
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM video_assets"
            " WHERE id = :id AND owner_sub = :o AND analysis_token = :tok"
            " FOR UPDATE",
            id=asset_id, o=owner, tok=token,
        )
        if cur.fetchone() is None:
            # 映像が消えたのか、権利が移ったのかを分ける
            cur.execute(
                "SELECT 1 FROM video_assets WHERE id = :id AND owner_sub = :o",
                id=asset_id, o=owner,
            )
            if cur.fetchone() is None:
                raise LookupError(asset_id)
            raise video.AnalysisSupersededError(asset_id)

        cur.execute("DELETE FROM video_scenes WHERE asset_id = :id", id=asset_id)
        cur.executemany(
            """
            INSERT INTO video_scenes(id, asset_id, start_ms, end_ms, thumb_object,
                                     source)
            VALUES (:id, :asset, :s, :e, :thumb, 'ai')
            """,
            [
                {
                    "id": str(uuid.uuid4()), "asset": asset_id,
                    "s": s.start_ms, "e": s.end_ms, "thumb": t,
                }
                for s, t in zip(scenes, thumbs, strict=True)
            ],
        )
        cur.execute(
            "UPDATE video_assets SET duration_ms = :d, thumb_object = :t"
            " WHERE id = :id AND owner_sub = :o",
            d=info.duration_ms, t=thumbs[0] if thumbs else None,
            id=asset_id, o=owner,
        )
        conn.commit()
