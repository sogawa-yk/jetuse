"""場面の記述・映像全体の要約・埋め込み(VID-03 / specs/20 §3 の 3〜6)。

**内容の抽出は視覚 LLM に一本化する**(ADR-0032 決定1・2026-08-20 改訂)。当初は
「構造化 = OCI AI Vision / 記述 = 視覚 LLM」の2層だったが、実測で覆した:

  * AI Vision の `video-job` は大阪・シカゴとも 404(同じ資格情報で `analyze-image` は
    成功するので**権限ではなく機能が無い**)
  * `TEXT_DETECTION` は日本語テロップを読めない(`大阪` → `XRR`。gemini は正確)

要求13 の例は日本語のテロップ・地名なので、AI Vision では満たせない。**この
モジュールは AI Vision を呼ばない。** ただし `VIDEO_ASSETS.vision_state` は残し、
分析のたびに `'skipped'` を書く —— 列を消すと「なぜ通っていないのか」が後から辿れない。

**時刻は LLM に聞かない**(ADR-0032 決定3)。区間は `video_frames` が ffmpeg の実測から
確定させてある。LLM には「この区間に何が映っているか」だけを渡し、秒数・撮影日・
タイムコードを答えさせない。渡していない情報は作文になる。

**判らない項目は `unknown` で持つ**(specs/20 §1 / ADR-0032 決定5)。NULL は
「まだ分析していない」、`unknown` は「分析したが判らなかった」。もっともらしい既定値で
埋めると、後から「AI が言ったのか、値が無いのか」が判別できなくなる。

**失敗を握りつぶさない**(specs/20 §3)。場面ごとの記述・要約・埋め込みのどれかが
落ちたら `partial` と理由、何も取れなければ `failed` と理由を台帳へ残す。
"""

import array
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import video, video_frames
from .db import connect

logger = logging.getLogger("jetuse.video_analyze")


# --- 定数(根拠つき) -----------------------------------------------------------

# 記述に使う視覚 LLM。`docunderstand.VLM_MODELS`(実測済みの vision 対応モデル)と同じ鍵。
# 日本語テロップを正しく読めることを 2026-08-20 に実測している(ADR-0032 決定1)。
DESCRIBE_MODEL = "google.gemini-2.5-pro"

# 「分析したが判らなかった」。**空文字や既定値で埋めない**(specs/20 §1)。
UNKNOWN = "unknown"

# migration 023 の CHECK 制約と同じ集合。LLM の返した値がこの外に出たら `unknown` へ
# 倒す —— 制約違反にすると、分析の失敗理由が「LLM が別の語を返した」ではなく
# 「INSERT が落ちた」になってしまう。
INDOOR_VALUES = frozenset({"indoor", "outdoor", UNKNOWN})
TIME_OF_DAY_VALUES = frozenset({"day", "night", UNKNOWN})

# 列幅(migration 023)。DB へ渡す前に収める。
PLACE_MAX = 255
SCENE_KIND_MAX = 64
WEATHER_MAX = 64
# 配列の要素と件数の上限。LLM が延々と語を並べても検索に効かず、CLOB を膨らませるだけ。
ITEM_MAX = 100
LIST_MAX = 30

# 1 回の分析で記述する場面数の上限。**超えた分は記述せず、`partial` と理由を残す**
# (黙って打ち切らない)。v1 は短い映像で成立させると決めてある(ADR-0032「v1 の外」:
# 長時間映像・大量映像の一括処理)。場面 1 つにつき視覚 LLM を 1 回呼ぶので、
# 2 時間の映像(最長 30 秒区間で 240 場面)を無制限に流すと費用も所要時間も青天井になる。
# 60 = 最長区間 30 秒で 30 分ぶん。
MAX_SCENES = 60

# 記述の同時実行数。`docunderstand.OCR_CONCURRENCY` と同じ考え方で、待ち時間の大半が
# LLM 応答なので少し重ねる。上げすぎるとサービス側で 429 になる。
DESCRIBE_CONCURRENCY = 4

# 要約の長さ(文字)。台帳の `summary` は CLOB だが、一覧に出す読み物なので短く保つ。
SUMMARY_MAX = 1200

# 1 回の応答で許す出力トークン。**思考ぶんを含む** —— `gemini-2.5-pro` は推論モデルで、
# 2026-08-20 の実測では `max_tokens=1024` の要約が文の途中で切れた(思考で使い切った)。
# 要求する本文(場面の JSON / 3〜5 文の要約)自体は 1000 トークンに満たないので、
# 余白は思考ぶん。切れた応答は `_content` が弾く(黙って保存しない)。
MAX_OUTPUT_TOKENS = 4096

_DESCRIBE_PROMPT = """あなたは1本の映像の**ある1区間**から抜き出した静止画
(時間順に数枚)を見ています。見えているものだけを答えてください。

次のキーだけを持つ JSON オブジェクトを1つだけ返します。前後に説明やコードフェンスは付けません。

{
  "description": "この場面に何が映っているかの説明(日本語・2〜3文)",
  "tags": ["検索に使う短い語"],
  "objects": ["映っている物体"],
  "people": {"present": "yes | no | unknown", "count": 人数(整数) または "unknown"},
  "place": "場所。判らなければ unknown",
  "scene_kind": "スタジオ / 屋外 / 道路 / 建物内 などの種別。判らなければ unknown",
  "indoor": "indoor / outdoor / unknown のいずれか",
  "time_of_day": "day / night / unknown のいずれか",
  "weather": "天候。判らなければ unknown",
  "actions": ["起きている行動・出来事"],
  "screen_text": ["画面に見えている文字を、見えるとおりに1行ずつ"]
}

必ず守ること:
- **判らない項目は "unknown" と書く。** もっともらしい値で埋めない。屋内か屋外か、
  昼か夜か、天候が何かが画面から判断できないなら unknown です。
  配列は該当が無ければ空配列 [] にする(語を作らない)。
- **時刻・経過秒数・タイムコード・撮影日を答えない。** 画像から判る情報ではありません。
  (画面に時計やテロップとして写っている文字は screen_text へ書き写してよい)
- 地名・人名・組織名などの固有名は、**画面に文字として書かれている場合か、
  明らかに判別できる場合だけ**書く。推測した固有名を place や description に書かない。
- screen_text は**書き写す**もので、翻訳・要約・補完をしない。日本語はそのまま日本語で
  書く。文字が無ければ []、文字はあるが読み取れなければ ["unknown"]。
"""

_SUMMARY_PROMPT = """次は1本の映像を時間順に区切った、各場面の説明です。
この映像全体が何の映像かを日本語で3〜5文にまとめてください。

必ず守ること:
- 場面の説明に書かれていないことを足さない(推測・一般論・時刻・撮影日を書かない)。
- 見出しや箇条書きにせず、本文だけを返す。
"""


class VideoAnalyzeError(RuntimeError):
    """分析の失敗。**理由なしでは投げない**(specs/20 §3)。"""


class SceneDescribeError(VideoAnalyzeError):
    """1 場面の記述に失敗した。他の場面は続行し、最後に `partial` になる。

    こちらは**応答の中身の問題**(JSON でない / `description` が無い)。利用者から見れば
    「もう一度分析すれば直るかもしれない」もので、直し方は再分析か映像の差し替え。
    """


class VisionServiceError(SceneDescribeError):
    """視覚 LLM を**呼べなかった**(認証・設定不備・429・タイムアウト・サービス障害)。

    応答の中身の問題(`SceneDescribeError`)と分けるのは、**直し方が違う**から。
    こちらは上流側の障害で、映像を差し替えても直らない —— 利用者に「入力が悪い」と
    見せると、直せないものを直そうとさせることになる(API は 502 に対応させる)。
    1 場面だけなら他の場面は続行するので `SceneDescribeError` の一種にしてある。
    """


# --- LLM 応答の解釈(純粋。ここだけで単体テストできる) -------------------------


def parse_json_object(raw: str | None) -> dict[str, Any]:
    """LLM 出力から JSON オブジェクトを取り出す。取り出せなければ例外。

    **形が違うものを「空の結果」に化けさせない。** ここで `{}` を返すと、呼び出し側は
    全項目が `unknown` の場面として保存してしまい、「LLM が壊れた応答を返した」ことが
    利用者にも記録にも残らない(specs/20 §3「握りつぶさない」)。

    コードフェンスと前後の地の文だけは剥がす —— モデルは指示しても付けることがあり、
    これは応答の壊れではなく体裁の揺れ。
    """
    text = (raw or "").strip()
    if not text:
        raise SceneDescribeError("視覚 LLM の応答が空でした")
    if text.startswith("```"):
        # ```json ... ``` / ``` ... ```
        parts = text.split("```")
        text = parts[1] if len(parts) >= 2 else text.strip("`")
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
        text = text.strip()
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        # 前後に地の文が付いた場合の最後の手当て。最初の { から最後の } まで
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise SceneDescribeError(
                f"視覚 LLM の応答が JSON ではありません: {text[:120]}"
            ) from None
        try:
            obj = json.loads(text[start:end + 1])
        except (json.JSONDecodeError, ValueError) as e:
            raise SceneDescribeError(
                f"視覚 LLM の応答を JSON として読めません: {text[:120]}"
            ) from e
    if not isinstance(obj, dict):
        raise SceneDescribeError(
            f"視覚 LLM の応答が JSON オブジェクトではありません: {type(obj).__name__}"
        )
    return obj


def _text(value: Any, limit: int, *, default: str = UNKNOWN) -> str:
    """文字列項目を1つ。**無い / 空 / 型違いは既定値(`unknown`)**。埋め合わせはしない。"""
    if isinstance(value, bool):
        return default
    if isinstance(value, int | float):
        value = str(value)
    if not isinstance(value, str):
        return default
    trimmed = value.strip()
    return trimmed[:limit] if trimmed else default


def _string_list(value: Any) -> list[str]:
    """文字列の配列。**判らないときは空**(配列に「不明」という第3の状態は作らない)。

    要素が 0 件でも `unknown` を1つ入れたりしない —— 検索のタグに `unknown` が並ぶと、
    「不明」という語で無関係な場面が引っかかる。
    """
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, bool):
            continue
        if isinstance(item, int | float):
            item = str(item)
        if isinstance(item, str) and item.strip():
            out.append(item.strip()[:ITEM_MAX])
    return out[:LIST_MAX]


def _enum(value: Any, allowed: frozenset[str]) -> str:
    """CHECK 制約のある項目。集合の外はすべて `unknown`(制約違反にしない)。"""
    if isinstance(value, str) and value.strip().lower() in allowed:
        return value.strip().lower()
    return UNKNOWN


def _people(value: Any) -> dict[str, Any]:
    """人物の有無と人数。**判らない側を潰さない**(`no` と `unknown` を混ぜない)。"""
    present, count = UNKNOWN, UNKNOWN
    if isinstance(value, dict):
        present = _enum(value.get("present"), frozenset({"yes", "no", UNKNOWN}))
        raw = value.get("count")
        if isinstance(raw, bool):
            pass
        elif isinstance(raw, int) and raw >= 0:
            count = raw
        elif isinstance(raw, str) and raw.strip().isdigit():
            count = int(raw.strip())
    elif isinstance(value, bool):
        present = "yes" if value else "no"
    return {"present": present, "count": count}


def normalize_scene(obj: dict[str, Any]) -> dict[str, Any]:
    """LLM の JSON を **DB の列に入る形**へ寄せる。欠けた項目は `unknown` / 空配列。

    `description` だけは欠けても `unknown` にしない。説明の無い場面は検索でも画面でも
    役に立たず、それを「分析済み」として保存すると、取れなかったことが記録に残らない。
    その場面は失敗として数え、映像全体は `partial` になる。

    `screen_text` は改行区切りの1つの文字列にする(列は CLOB)。**文字が無ければ NULL**。
    Oracle は空文字を NULL として格納するため、「文字が無かった」を空文字で表せない。
    `"none"` のような番兵を入れると、VID-04 の文字検索でその語が誤って当たる
    (自由文の列なので利用者の検索語とぶつかる)。よって、分析済みの行で NULL は
    「画面に文字が無かった」、`"unknown"` は「文字はあるが読めなかった」と読む。
    未分析かどうかは `analysis_state` と `description` の有無で判る。
    """
    description = _text(obj.get("description"), 4000, default="")
    if not description:
        raise SceneDescribeError("視覚 LLM の応答に description がありません")

    screen_lines = _string_list(obj.get("screen_text"))
    return {
        "description": description,
        "tags": _string_list(obj.get("tags")),
        "objects": _string_list(obj.get("objects")),
        "people": _people(obj.get("people")),
        "actions": _string_list(obj.get("actions")),
        "place": _text(obj.get("place"), PLACE_MAX),
        "scene_kind": _text(obj.get("scene_kind"), SCENE_KIND_MAX),
        "indoor": _enum(obj.get("indoor"), INDOOR_VALUES),
        "time_of_day": _enum(obj.get("time_of_day"), TIME_OF_DAY_VALUES),
        "weather": _text(obj.get("weather"), WEATHER_MAX),
        "screen_text": "\n".join(screen_lines) or None,
    }


def embedding_text(scene: dict[str, Any]) -> str:
    """埋め込みに載せる文字列(specs/20 §3-6: 場面説明 + タグ + 文字)。

    `unknown` は載せない —— 「不明」という語がベクトルに混ざると、判らない場面どうしが
    互いに近くなり、検索の順位を歪める。物体・行動も検索の当たりどころなので入れる。
    """
    parts = [scene["description"]]
    for key in ("tags", "objects", "actions"):
        parts += scene[key]
    for key in ("place", "scene_kind", "weather"):
        if scene[key] != UNKNOWN:
            parts.append(scene[key])
    if scene["screen_text"] and scene["screen_text"] != UNKNOWN:
        parts.append(scene["screen_text"])
    return " / ".join(p for p in parts if p)


# --- 視覚 LLM の呼び出し ------------------------------------------------------


def _content(resp: Any, error: type[VideoAnalyzeError]) -> str:
    """応答本文。**上限で切れた応答を使わない**。

    `gemini-2.5-pro` は推論モデルで、思考ぶんも `max_tokens` に数える(2026-08-20 実測:
    `max_tokens=1024` の要約が文の途中 —— 「次に、コンピュータ」—— で切れた)。切れた
    まま保存すると、**壊れた要約が「分析済み」として残る**。記述側は JSON が閉じないので
    parse で落ちるが、要約は素の文なので気づけない。ここで `finish_reason` を見て弾く。

    **形の違う応答で素の例外を漏らさない。** `choices` が空・`message` が無い・本文が
    文字列でない、はいずれも上流の応答が想定と違うという同じ話で、呼び出し側は
    `VideoAnalyzeError` の系統しか部分失敗として扱わない。ここで `IndexError` や
    `AttributeError` を漏らすと、**要約の応答が壊れているだけで分析全体が `failed` に
    なり、成功していた場面の記述ごと捨てられる**。
    """
    choices = getattr(resp, "choices", None) or []
    if not choices:
        raise error("応答に choices がありません")
    choice = choices[0]
    if getattr(choice, "finish_reason", None) == "length":
        raise error(
            f"応答が出力上限({MAX_OUTPUT_TOKENS} トークン)で切れました"
            "(推論モデルは思考ぶんも同じ上限を使う)"
        )
    message = getattr(choice, "message", None)
    content = getattr(message, "content", None)
    if content is None:
        raise error("応答に本文がありません")
    if not isinstance(content, str):
        raise error(f"応答の本文が文字列ではありません: {type(content).__name__}")
    return content


def _client(timeout: float = 180.0):
    from .genai import make_inference_client

    return make_inference_client(timeout=timeout)


def describe_scene(frames: list[bytes], *, model: str = DESCRIBE_MODEL) -> dict[str, Any]:
    """1 場面の代表フレームを視覚 LLM へ渡し、正規化した場面メタデータを返す。

    **渡すのは画像だけ。** 開始・終了時刻も、前後の場面も、映像の題名も渡さない ——
    渡していない情報を答えさせないため(ADR-0032 決定1)。
    """
    import base64

    if not frames:
        raise SceneDescribeError("代表フレームが 1 枚もありません")
    content: list[dict[str, Any]] = [{"type": "text", "text": _DESCRIBE_PROMPT}]
    for jpeg in frames:
        b64 = base64.b64encode(jpeg).decode()
        content.append(
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
        )
    try:
        resp = _client().chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": content}],
            # 揺らぎを最小にする。同じ画から毎回違う説明が出ると、再分析するたびに
            # 検索結果が変わって「直したのか揺れたのか」が判らなくなる
            temperature=0,
            max_tokens=MAX_OUTPUT_TOKENS,
        )
    except Exception as e:  # noqa: BLE001 — モデル不可・認証・429・タイムアウト等
        raise VisionServiceError(f"視覚 LLM の呼び出しに失敗: {str(e)[:200]}") from e
    return normalize_scene(parse_json_object(_content(resp, SceneDescribeError)))


def summarize(descriptions: list[str], *, model: str = DESCRIBE_MODEL) -> str:
    """映像全体の要約(specs/20 §3-5 / 要求9)。場面の説明だけから作る。"""
    if not descriptions:
        raise VideoAnalyzeError("要約する場面の説明がありません")
    body = "\n".join(f"- {d}" for d in descriptions)
    try:
        resp = _client(timeout=120.0).chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": f"{_SUMMARY_PROMPT}\n\n{body}"}],
            temperature=0,
            max_tokens=MAX_OUTPUT_TOKENS,
        )
    except Exception as e:  # noqa: BLE001
        raise VisionServiceError(f"要約の生成に失敗: {str(e)[:200]}") from e
    text = _content(resp, VideoAnalyzeError).strip()
    if not text:
        raise VideoAnalyzeError("要約が空でした")
    if len(text) > SUMMARY_MAX:
        # **ここで切らない。** 上限で切れた応答を弾いておきながら、長い応答を自分で
        # 文の途中から切り落としては同じことになる。3〜5 文を頼んで 1200 文字を超えるのは
        # 指示どおりに答えていない応答なので、`partial` の理由として残す
        raise VideoAnalyzeError(
            f"要約が長すぎます({len(text)} 文字 > {SUMMARY_MAX})。"
            "途中で切って保存すると壊れた要約が残る"
        )
    return text


# --- 台帳への書き込み ---------------------------------------------------------


def _json(value: Any) -> str:
    """JSON 列(`IS JSON` 制約つき)へ入れる文字列。日本語はそのまま持つ。"""
    return json.dumps(value, ensure_ascii=False)


def _begin_analysis(owner: str, asset_id: str, token: str) -> None:
    """権利を取った直後に台帳を「この分析の結果はまだ無い」状態へ揃える。

    `vision_state = 'skipped'` —— AI Vision 層を通っていないことを残すための列
    (ADR-0032 決定1)。NULL(触れていない)のままにすると、「使わないと決めた」のか
    「実装が呼び忘れている」のかが後から判らない。失敗して終わる分析でも記録が要るので、
    ここで書く。

    `summary = NULL` —— **前回の要約を今回の結果として見せない。** 再分析で要約に
    失敗すると `summary` は書かれない(`_save_analysis` は None を書かない)ので、
    消さないと `analysis_state` が `partial` / `failed` なのに前回成功時の要約が
    そのまま残る。NULL は「まだ無い」の意味なので、ここで戻すのが筋が通る。

    **場面も同じ 1 トランザクションで消す**(VID-03 レビューの major)。要約だけを消すと、
    新しい場面を保存する前(映像の取得・ffmpeg・場面分割)で落ちたときに、**前回の
    description / tags / screen_text / embedding が残ったまま `analysis_state` だけ
    今回のものへ動く**。台帳を読んだ側は「いつの分析結果か」を区別できず、検索
    (specs/20 §4)は消えたはずの前回のベクトルで当て続ける。
    **世代を揃えて置き換える** —— 今回の分析が始まった時点で、前の世代の結果は無い。

    代償として、再分析が途中で落ちると前回の場面も残らない。それでよい:
    `analysis_state` = `failed` + 理由(specs/20 §3)が「取れなかった」を示すのに対し、
    古い場面を残すと画面には**成功したときと同じ見た目**で前回の結果が並ぶ。
    「分析済み」と「分析したが取れなかった」を同じ表示にしない、が仕様の要求。

    権利の印が合わなければ**何も消さずに降りる**。ここは破壊的なので、
    `UPDATE` の rowcount を必ず見る —— 引き継がれた側がそのまま DELETE を撃つと、
    新しい実行が入れたばかりの場面を消してしまう。
    """
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE video_assets SET vision_state = 'skipped', summary = NULL"
            " WHERE id = :id AND owner_sub = :o AND analysis_token = :tok",
            id=asset_id, o=owner, tok=token,
        )
        if cur.rowcount == 0:
            cur.execute(
                "SELECT 1 FROM video_assets WHERE id = :id AND owner_sub = :o",
                id=asset_id, o=owner,
            )
            if cur.fetchone() is None:
                raise LookupError(asset_id)
            raise video.AnalysisSupersededError(asset_id)
        cur.execute("DELETE FROM video_scenes WHERE asset_id = :id", id=asset_id)
        conn.commit()


def _scene_rows(asset_id: str) -> list[dict[str, Any]]:
    """`video_frames` が作った場面を時間順に。**id はここで引く**。

    分割は `DELETE` + `INSERT` で作り直すので、id は分割のたびに変わる。分割の戻り値は
    区間とサムネイルしか持たないため、記述を書き戻す先はここで引き直す。
    """
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, start_ms, end_ms, thumb_object FROM video_scenes"
            " WHERE asset_id = :id ORDER BY start_ms",
            id=asset_id,
        )
        return [
            {"id": r[0], "start_ms": r[1], "end_ms": r[2], "thumb_object": r[3]}
            for r in cur.fetchall()
        ]


def _save_analysis(
    owner: str,
    asset_id: str,
    token: str,
    described: list[tuple[str, dict[str, Any], array.array | None]],
    summary: str | None,
) -> None:
    """場面の記述・埋め込みと映像の要約を1トランザクションで書く。

    **権利の印を `FOR UPDATE` で照合してから書く**(`video_frames._save_scenes` と同じ)。
    印が合わなければ、この実行は別の実行に引き継がれた側なので 1 行も書かずに降りる。
    途中でコミットを分けると、説明だけ入って要約が無い状態が残りうる。
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
            cur.execute(
                "SELECT 1 FROM video_assets WHERE id = :id AND owner_sub = :o",
                id=asset_id, o=owner,
            )
            if cur.fetchone() is None:
                raise LookupError(asset_id)
            raise video.AnalysisSupersededError(asset_id)

        for scene_id, meta, vector in described:
            cur.execute(
                """
                UPDATE video_scenes
                   SET description = :d, tags = :tags, objects = :objects,
                       people = :people, actions = :actions, place = :place,
                       scene_kind = :kind, indoor = :indoor, time_of_day = :tod,
                       weather = :weather, screen_text = :text,
                       embedding = :emb
                 WHERE id = :id AND asset_id = :asset
                """,
                d=meta["description"], tags=_json(meta["tags"]),
                objects=_json(meta["objects"]), people=_json(meta["people"]),
                actions=_json(meta["actions"]), place=meta["place"],
                kind=meta["scene_kind"], indoor=meta["indoor"],
                tod=meta["time_of_day"], weather=meta["weather"],
                text=meta["screen_text"],
                # ベクトルは `_embed_scenes` が検証済み(`embeddings.as_vector`)。
                # ここで変換しないのは、壊れた値の例外をこの 1 トランザクションの
                # 途中で上げないため —— 場面の記述ごと巻き添えで落ちる
                emb=vector,
                id=scene_id, asset=asset_id,
            )
        if summary is not None:
            cur.execute(
                "UPDATE video_assets SET summary = :s"
                " WHERE id = :id AND owner_sub = :o",
                s=summary, id=asset_id, o=owner,
            )
        conn.commit()


# --- 分析の本体 ---------------------------------------------------------------


def _describe_all(
    path: Any, scenes: list[dict[str, Any]], model: str
) -> tuple[list[tuple[str, dict[str, Any]]], list[Exception]]:
    """場面ごとに代表フレームを抜いて記述する。**1 場面の失敗で全体を止めない**。

    返すのは (成功した (scene_id, メタデータ), 失敗した場面の例外)。**理由を文字列に
    潰さず例外のまま返す** —— 呼び出し側が「上流が落ちているのか、応答の中身が
    おかしいのか」を種類で見分けて HTTP 状態まで伝えるため。

    フレーム抽出は ffmpeg(安く速い)なので順に、記述は LLM 待ちが大半なので少し重ねる。
    **`FfmpegUnavailableError` はここで拾わない** —— ffmpeg が起動できないのは配備の
    不備で、次の場面でも必ず同じところで落ちる。場面数ぶん同じ失敗を数えても意味がない。
    """
    prepared: list[tuple[dict[str, Any], list[bytes]]] = []
    failures: list[Exception] = []
    for row in scenes:
        scene = video_frames.Scene(row["start_ms"], row["end_ms"])
        try:
            frames = video_frames.scene_frames(path, scene)
        except video_frames.FfmpegUnavailableError:
            raise
        except Exception as e:  # noqa: BLE001 — 壊れた区間 1 つで映像全体を捨てない
            failures.append(
                video_frames.VideoFrameError(
                    f"{row['start_ms']}ms: フレーム抽出に失敗: {e}"
                )
            )
            continue
        prepared.append((row, frames))

    def run(item: tuple[dict[str, Any], list[bytes]]):
        row, frames = item
        try:
            return row, describe_scene(frames, model=model), None
        except SceneDescribeError as e:
            # 種類(上流の障害 / 応答の中身)を保ったまま、どの場面かだけ足す
            return row, None, type(e)(f"{row['start_ms']}ms: {e}")
        except Exception as e:  # noqa: BLE001 — 想定外でも次の場面へ進む
            return row, None, VideoAnalyzeError(f"{row['start_ms']}ms: {e}")

    described: list[tuple[str, dict[str, Any]]] = []
    if prepared:
        with ThreadPoolExecutor(
            max_workers=min(DESCRIBE_CONCURRENCY, len(prepared))
        ) as ex:
            for row, meta, error in ex.map(run, prepared):
                if meta is None:
                    failures.append(error)
                else:
                    described.append((row["id"], meta))
    return described, failures


def _embed_scenes(
    described: list[tuple[str, dict[str, Any]]]
) -> tuple[list[array.array | None], str | None]:
    """場面ごとの埋め込み(`cohere.embed-multilingual-v3.0` / 1024 次元)。

    落ちても記述は保存する。**埋め込みだけ失敗した場面をベクトル無しで残す**ほうが、
    説明ごと捨てるより利用者の役に立つ(検索に出ないことは `partial` の理由で判る)。

    **件数だけでなく値も検べる**(`embeddings.as_vector` / VID-03 レビューの major)。
    非数値・NaN/Infinity・次元違いをそのまま通すと、`array.array` 変換か DB の UPDATE で
    素の例外になり、上の「記述は保存して `partial`」が破れて分析全体が落ちる。
    壊れていたのがどの場面かは理由に残す —— 黙って全部をベクトル無しにしない。
    """
    if not described:
        return [], None
    from .embeddings import as_vector, embed

    try:
        vectors = embed([embedding_text(m) for _, m in described])
    except Exception as e:  # noqa: BLE001
        return [None] * len(described), f"埋め込みの生成に失敗: {str(e)[:200]}"
    if len(vectors) != len(described):
        return (
            [None] * len(described),
            f"埋め込みの件数が場面数と合いません({len(vectors)} != {len(described)})",
        )
    out: list[array.array | None] = []
    broken: list[str] = []
    for (_, meta), vector in zip(described, vectors, strict=True):
        try:
            out.append(as_vector(vector))
        except ValueError as e:
            out.append(None)
            broken.append(f"{meta['description'][:20]}…: {e}")
    if broken:
        return out, (
            f"埋め込みの値が使えない場面が {len(broken)} 件ありました: "
            + " / ".join(broken[:3])
        )
    return out, None


def analyze_asset(
    owner: str,
    asset_id: str,
    *,
    model: str = DESCRIBE_MODEL,
    threshold: float = video_frames.SCENE_THRESHOLD,
) -> dict[str, Any]:
    """映像 1 本を分析する(specs/20 §3。再分析も同じ入口 = 要求8)。

    権利を1回取る → 場面を割る(VID-02) → 場面ごとに視覚 LLM で記述 → 映像全体の要約 →
    埋め込み → まとめて保存 → `done` / `partial` / `failed` を理由つきで書く。

    **入口は 1 本**(specs/20 §3「同時実行の範囲」)。`video.claim_analysis` の
    アトミックな UPDATE で権利を取り、取れなければ `AnalysisInProgressError`(API は 409)。
    以降の書き込みは権利の印(`token`)が台帳と一致するときだけ通る。引き継がれた側は
    何も書かずに `AnalysisSupersededError` で降りる。

    **分析の実行中にその映像が削除された場合の即時整合は取らない**(specs/20 §3。
    残骸は `video_frames.reap_orphan_assets` が後から回収する)。
    """
    token = video.claim_analysis(owner, asset_id)
    try:
        result = _analyze_claimed(owner, asset_id, token, model=model, threshold=threshold)
    except video.AnalysisSupersededError:
        # 引き継がれた側。**何も書かずに降りる** —— 新しい実行の running を解かない
        logger.warning("video analysis was superseded: %s", asset_id)
        raise
    except Exception as e:
        reason = f"{type(e).__name__}: {e}"
        if not video.finish_analysis(owner, asset_id, "failed", reason, token=token):
            logger.warning(
                "video analysis was superseded; not recording failure: %s", asset_id
            )
        raise

    state = result["analysis_state"]
    if not video.finish_analysis(
        owner, asset_id, state, result["analysis_error"], token=token
    ):
        raise video.AnalysisSupersededError(asset_id)
    return result


def _analyze_claimed(
    owner: str, asset_id: str, token: str, *, model: str, threshold: float
) -> dict[str, Any]:
    """権利を取った状態で走る本体。状態と理由を組み立てて返す(書くのは呼び出し側)。"""
    _begin_analysis(owner, asset_id, token)

    failures: list[str] = []
    with video_frames.open_asset_video(owner, asset_id) as path:
        split = video_frames.split_claimed_scenes(
            owner, asset_id, token, threshold=threshold, path=path
        )
        all_scenes = _scene_rows(asset_id)
        if not all_scenes:
            # 分割は必ず 1 件以上作る(`build_scenes`)。0 件はこちらの取り違え
            raise VideoAnalyzeError("場面が 1 件も作られませんでした")
        # **件数の意味を混ぜない。** `scene_count` は映像に在る場面の総数、
        # `described_count` は記述できた数。切り詰めた側を総数として返すと、
        # 応答だけを見た利用者には「この映像には 60 場面しかない」と映る
        scenes = all_scenes[:MAX_SCENES]
        if len(all_scenes) > len(scenes):
            # **黙って打ち切らない。** 何件を記述しなかったかを理由として残す
            failures.append(
                f"場面が {len(all_scenes)} 件あるため先頭 {MAX_SCENES} 件だけを"
                f"記述しました(長時間映像は v1 の範囲外 / ADR-0032)"
            )
        described, scene_failures = _describe_all(path, scenes, model)
    failures += [str(e) for e in scene_failures]

    if not described:
        reason = "どの場面も記述できませんでした: " + " / ".join(failures[:5])
        # **上流の障害を「入力が悪い」に見せない。** 全滅の原因が視覚 LLM を呼べない
        # ことなら、映像を差し替えても直らない(API は 502 に対応させる)
        if scene_failures and all(
            isinstance(e, VisionServiceError) for e in scene_failures
        ):
            raise VisionServiceError(reason)
        raise VideoAnalyzeError(reason)

    vectors, embed_failure = _embed_scenes(described)
    if embed_failure:
        failures.append(embed_failure)

    summary: str | None = None
    try:
        summary = summarize([m["description"] for _, m in described], model=model)
    except VideoAnalyzeError as e:
        failures.append(str(e))

    _save_analysis(
        owner, asset_id, token,
        [(sid, meta, vec) for (sid, meta), vec in zip(described, vectors, strict=True)],
        summary,
    )

    described_by_id = dict(described)
    return {
        "asset_id": asset_id,
        "analysis_state": "partial" if failures else "done",
        # `partial` は理由を必ず伴う(`video.finish_analysis` が検証する)
        "analysis_error": " / ".join(failures)[:4000] if failures else None,
        "vision_state": "skipped",
        "duration_ms": split["duration_ms"],
        "summary": summary,
        "scene_count": len(all_scenes),
        "truncated_scene_count": len(all_scenes) - len(scenes),
        "described_count": len(described),
        "scenes": [
            {**row, **described_by_id.get(row["id"], {}),
             "described": row["id"] in described_by_id}
            for row in all_scenes
        ],
    }
