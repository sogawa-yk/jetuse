"""映像の保管と登録(VID-01 / specs/20 §1 §2)。

**映像本体は Object Storage、台帳は ADB**。DB に本体は入れない(tasks/VID-01.md 禁止事項)。
再生は PAR(期限付き URL)で行う —— API が映像を中継すると Container Instance の
メモリと帯域を食い、シーク再生(Range)も自前で実装することになる。

所有者分離は既存の流儀(`rag` / `minutes` / `demos`)に合わせ、**SQL の
`WHERE owner_sub = :o` で強制**する。他人の映像は 403 ではなく「存在しない」扱い
(所有者以外に id の存在有無を漏らさない)。
"""

import logging
import mimetypes
import pathlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, BinaryIO

from .db import connect
from .settings import get_settings

logger = logging.getLogger("jetuse.video")

# 画面の選択肢と揃える。ここが狭いと画面から試せない(rag の ALLOWED_EXTENSIONS と同じ考え)
ALLOWED_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
# v1 は短い映像で成立させる(ADR-0032 範囲)。Container Instance 4GB を前提に上限を置く
MAX_BYTES = 500 * 1024 * 1024

# **API Gateway の本文上限**(2026-08-20 実測: 20 MiB ちょうど。20,971,199 バイトは通り
# 20,971,720 バイトが 413)。ゲートウェイを通る multipart 経路(`POST /api/video/assets`)は
# ここを越えられない —— **アプリが 500MB を許しても、要求はアプリに届かない**。
GATEWAY_MAX_BODY_BYTES = 20 * 1024 * 1024

# multipart 経路で受け付けるファイル本体の上限。上の上限は**本文全体**(メタデータの
# フォーム項目と multipart の縁取りを含む)に効くので、その分を引いておく。64KiB は
# 題名・所属・権利(合計 2KB 弱)と縁取りを十分に上回る幅。
# **実態と違う案内をしない**(tasks/VID-07 禁止事項)。ここが 500MB のままだと、画面は
# 「500MB まで」と言いながら 20MB で 413 になる —— 利用者は原因を辿れない。
MULTIPART_MAX_BYTES = GATEWAY_MAX_BODY_BYTES - 64 * 1024

# --- 直接アップロード(VID-07 / specs/20 §2) -----------------------------------

# アップロード用 PAR の寿命。**短命にする**(tasks/VID-07 禁止事項)。ただし 100MB 級を
# 細い上り回線で上げ切るには時間がかかり、PUT は 1 本の要求なので途中で期限が切れると
# 上げ直しになる。1 時間は「上げ切れる幅」と「配ったまま長く残さない」の折衷。
UPLOAD_TTL_SECONDS = 3600

# アップロード用 PAR の名前。**再生用(`jetuse-video-`)と別の接頭辞にする** ——
# 名前で用途が判らないと、棚卸ししたときに書き込み用が残っていても気づけない。
_UPLOAD_PAR_NAME_PREFIX = "jetuse-video-upload-"

# `uploading` のまま残った行を回収してよくなるまでの時間。**PAR の寿命より長く取る** ——
# まだ有効な PAR で上げている最中の行を消すと、上げ切った直後の complete が 404 になる。
UPLOAD_STALE_SECONDS = 2 * UPLOAD_TTL_SECONDS

# 再生 URL の既定寿命と天井。**無期限の PAR を作らない**。
# 天井を超える指定はクランプせず拒否する(黙って縮めると「延ばしたのに効かない」になる)
PLAYBACK_TTL_SECONDS = 3600
PLAYBACK_TTL_MAX = 24 * 3600

# 一覧のページング上限。天井超過はクランプする(一覧は取得件数が減るだけで意味が壊れない)
LIST_LIMIT_MAX = 100

_PAR_NAME_PREFIX = "jetuse-video-"

# 取り残された `running` を引き継いでよくなるまでの時間(specs/20 §3「同時実行の範囲」)。
# 分析中にプロセスが落ちると `analysis_state` は `running` のまま残る。開始時刻を見ずに
# `<> 'running'` だけで弾くと、その映像は**二度と再分析できない**(要求8 が死ぬ)。
# 2 時間は、v1 が対象とする短い映像の分析(ffmpeg の 1 パス + 場面数ぶんのフレーム抽出。
# 個々の上限は video_frames の SCAN_TIMEOUT_S / FRAME_TIMEOUT_S)を十分に上回る幅。
ANALYSIS_STALE_SECONDS = 2 * 3600

# 分析の状態(migration 022 の `video_assets_state_ck` と同じ集合)。
# **ここに無い値は台帳へ書かせない。** 綴り違いをそのまま渡すと ORA-02290 になり、
# 失敗の理由が「分析が失敗した理由」から「状態の書き方を間違えた」へすり替わる。
ANALYSIS_STATES = frozenset({"pending", "running", "done", "failed", "partial"})

# **理由を必ず持つ状態。**「失敗を握りつぶさない」(specs/20 §3)は、理由の無い
# `failed` を保存できてしまうと成立しない。`partial` も同じ —— 何が取れなかったのかを
# 残さない `partial` は、画面上「分析済み」と見分けがつかない。
# 逆に `done` / `pending` / `running` に理由を残させない。前回の失敗理由が今回の結果に
# 混ざると、「いま失敗しているのか、前に失敗したのか」が判らなくなる。
ANALYSIS_STATES_WITH_REASON = frozenset({"failed", "partial"})

# `analysis_error VARCHAR2(4000)`(migration 022)の実効上限。**バイト**で効く(`_fit_bytes`)。
ANALYSIS_ERROR_MAX_BYTES = 4000

# 時刻列は **UTC で保存し、UTC として返す**(格納側は下記 to_utc_naive、既定値は
# migration の SYS_EXTRACT_UTC)。列は TIMESTAMP(タイムゾーン無し)なので、末尾の "Z" を
# 付けて「どの時間帯の値か」を明示する。付けないと "2026-08-19T10:00:00" が受け手の
# ローカル時刻と解釈されて 9 時間ずれる。**AT TIME ZONE は使わない** —— 素の TIMESTAMP に
# 掛けるとセッションの時間帯で解釈されてから変換され、UTC で入れた値が動く。
TS_UTC = "TO_CHAR({col}, 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')"
_ASSET_COLUMNS = (
    "id, title, " + TS_UTC.format(col="created_at") + ", duration_ms, "
    "collection, category, rights, " + TS_UTC.format(col="captured_at") + ", "
    "analysis_state, analysis_error, vision_state, object_name, thumb_object, summary"
)


# 場面の列と、行 → dict の変換は**1 か所に持つ**。詳細(get_asset)と編集(video_edit)が
# 別々に並べると、列を足したときに片方だけが返す形になる(利用者から見れば同じ「場面」)。
_SCENE_COLUMN_NAMES = (
    "id", "start_ms", "end_ms", "description", "tags", "objects", "people", "actions",
    "place", "scene_kind", "indoor", "time_of_day", "weather", "screen_text",
    "thumb_object", "source",
)


def scene_columns(prefix: str = "") -> str:
    """SELECT に並べる場面の列。`prefix` は結合時の別名(例 `"s."`)。"""
    return ", ".join(prefix + c for c in _SCENE_COLUMN_NAMES) + ", " + TS_UTC.format(
        col=prefix + "confirmed_at"
    )


def row_to_scene(row: tuple) -> dict[str, Any]:
    """`scene_columns()` の並びの行を dict に。

    JSON 列(tags/objects/people/actions)は**文字列のまま返す**。中身を作るのは分析側で、
    ここで parse すると壊れた値を API 全体の 500 に変えてしまう(`IS JSON` で入口は守られ、
    壊れていることは利用者が見て判る)。
    """
    scene = dict(zip(_SCENE_COLUMN_NAMES, row[:len(_SCENE_COLUMN_NAMES)], strict=True))
    scene["confirmed_at"] = row[len(_SCENE_COLUMN_NAMES)]
    return scene


# `scene_columns()` が並べる列の数(末尾の confirmed_at ぶんを足す)。結合した SELECT で
# 場面の後ろに別の列を足すとき、位置を数え直さずに済ませるため。
SCENE_COLUMN_COUNT = len(_SCENE_COLUMN_NAMES) + 1

_SCENE_COLUMNS = scene_columns()


class AnalysisInProgressError(RuntimeError):
    """その映像の分析が既に走っている(specs/20 §3)。API は 409 に対応させる。"""


class UploadIncompleteError(RuntimeError):
    """本体がまだ確定していない映像に、本体を要る操作を掛けた(VID-07)。

    2 段アップロードの中間状態(`upload_state = 'uploading'`)。API は 409 に対応させる ——
    404 にすると「無い」と読めてしまうが、行は在るし上げ直せば使える。
    """


class UploadVerificationError(RuntimeError):
    """上げられた実物が検証に落ちた(存在しない / 空 / 大きすぎる / 別の Content-Type)。

    **「入れたと言われたから入った」ことにしない**(tasks/VID-07)。落ちた時点で
    オブジェクト・PAR・台帳の行をまとめて片付けるので、呼び出し側は上げ直しになる。
    """


class AnalysisSupersededError(RuntimeError):
    """自分の権利が別の実行に引き継がれていた(取り残された `running` の引き継ぎ)。

    引き継がれた側が**何も書かずに降りる**ための合図。これが無いと、生きたまま
    引き継がれた古い実行が新しい実行の場面・状態を上書きし、「同じ映像への分析は
    同時に 1 つだけ」(specs/20 §3)が結果として破れる。
    """


def to_utc_naive(value: datetime | None) -> datetime | None:
    """aware な日時は UTC へ寄せ、tzinfo を落として返す(naive はそのまま UTC 扱い)。

    保存先は `TIMESTAMP`(タイムゾーン無し)。オフセット付きの値をそのまま渡すと、
    同じ瞬間でも入力のオフセット次第で別の壁時計時刻として保存され、後の期間検索
    (specs/20 §4 の captured_from/to)が静かにずれる。**入口で 1 つの時間帯に寄せる。**
    """
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def os_client():
    """映像バケットのある**リージョン**を向いた Object Storage クライアント。

    `config_file` モード(ローカル開発)では `sdk_signer_args` の region 引数は効かず、
    `~/.oci/config` のプロファイル値が使われる。バケットは配備リージョンにあるので、
    プロファイル任せだと別リージョンの端点へ投げて `BucketNotFound` になる
    (2026-08-19 実測: 大阪プロファイルからシカゴの `jetuse-pubdev-video` が引けなかった)。
    `genai.py` / `tts.py` と同じく config へ明示する。
    """
    import oci

    from .oci_auth import sdk_signer_args

    region = get_settings().oci_region
    args = sdk_signer_args(region)
    args["config"]["region"] = region  # config_file の config にも region を効かせる
    return oci.object_storage.ObjectStorageClient(**args)


def require_bucket() -> str:
    bucket = get_settings().video_bucket
    if not bucket:
        raise RuntimeError("VIDEO_BUCKET is not configured")
    return bucket


def _fit(value: str | None, limit: int) -> str | None:
    """列幅に収める。空文字は None(値なし)に寄せる。"""
    if value is None:
        return None
    value = value[:limit]
    return value or None


# 失敗理由を切り詰めるときの印。**黙って切らない**(途中で切れた文を全文と誤読させない)。
_TRUNCATED = "…(以下省略)"


def _fit_bytes(value: str | None, limit: int) -> str | None:
    """**バイト長で**列幅に収める。空文字は None(値なし)に寄せる。

    `analysis_error VARCHAR2(4000)`(migration 022)は CHAR 指定が無いので、既定では
    **BYTE セマンティクス**で効く。日本語 1 文字は UTF-8 で 3 バイトなので、文字数で
    切ると 4000 文字未満でも 4000 バイトを超えて `ORA-12899` になる。落ちる先が悪い ——
    ここは分析の失敗を記録する最後の 1 文なので、落ちると
    **理由が残らないうえ `analysis_state` が `running` のまま固まる**
    (`ANALYSIS_STALE_SECONDS` が経つまで再分析もできない)。場面数ぶんの日本語の理由を
    連ねる `video_analyze` では現実に届く長さ(60 場面 × 数十文字)。
    `rag_adb.doc_key` が `VARCHAR2(400)` で同じ理由からバイト長で切っているのに揃える。
    """
    if value is None:
        return None
    raw = value.encode("utf-8")
    if len(raw) > limit:
        keep = limit - len(_TRUNCATED.encode("utf-8"))
        # 途中のバイトで切れた文字は捨てる(errors="ignore")
        value = raw[:keep].decode("utf-8", "ignore") + _TRUNCATED
    return value or None


def content_type_for(filename: str) -> str:
    """再生できる Content-Type を付けて置く。

    付けないと Object Storage は `application/octet-stream` で返し、PAR の URL を
    開いてもブラウザが再生せずダウンロードになる(= 完了条件「再生できる」を満たさない)。
    """
    guessed, _ = mimetypes.guess_type(filename)
    return guessed if (guessed or "").startswith("video/") else "application/octet-stream"


def _row_to_asset(row: tuple) -> dict[str, Any]:
    return {
        "id": row[0], "title": row[1], "created_at": row[2], "duration_ms": row[3],
        "collection": row[4], "category": row[5], "rights": row[6],
        "captured_at": row[7], "analysis_state": row[8], "analysis_error": row[9],
        "vision_state": row[10], "object_name": row[11], "thumb_object": row[12],
        "summary": row[13],
    }


# --- 登録 ---------------------------------------------------------------------


def create_asset(
    owner: str,
    filename: str,
    data: BinaryIO,
    size: int,
    *,
    title: str | None = None,
    collection: str | None = None,
    category: str | None = None,
    rights: str | None = None,
    captured_at: datetime | None = None,
    duration_ms: int | None = None,
) -> dict[str, Any]:
    """映像を Object Storage へ置き、`pending` で台帳に載せる(specs/20 §2)。

    `data` はストリームのまま渡す(`UploadFile.file`)。バイト列に読み切ると
    映像 1 本分がそのままコンテナのメモリに載る。
    """
    bucket = require_bucket()
    client = os_client()
    ns = client.get_namespace().data

    asset_id = str(uuid.uuid4())
    ext = pathlib.Path(filename).suffix.lower()
    # キーに利用者の入力(ファイル名)を混ぜない。以降の操作は**台帳に記録した
    # object_name** だけを使い、組み立て直さない
    object_name = f"video/{owner}/{asset_id}/source{ext}"
    client.put_object(
        ns, bucket, object_name, data, content_type=content_type_for(filename)
    )

    # **正規化は 1 回だけ。** 保存する値と返す値を別々に作ると、POST の応答と直後の
    # GET で内容が変わる(列幅で切り詰めた分だけ食い違う)
    stored = {
        "title": _fit(title or filename, 500),
        "collection": _fit(collection, 255),
        "category": _fit(category, 255),
        "rights": _fit(rights, 1000),
        "captured_at": to_utc_naive(captured_at),
    }
    try:
        with connect() as conn:
            conn.cursor().execute(
                """
                INSERT INTO video_assets(id, owner_sub, title, collection, category,
                                         rights, captured_at, duration_ms, object_name,
                                         analysis_state)
                VALUES (:id, :o, :t, :coll, :cat, :rights, :captured, :dur, :obj,
                        'pending')
                """,
                id=asset_id, o=owner, t=stored["title"], coll=stored["collection"],
                cat=stored["category"], rights=stored["rights"],
                captured=stored["captured_at"], dur=duration_ms, obj=object_name,
            )
            conn.commit()
    except Exception:
        # 台帳に載らなかった本体は残さない(誰からも辿れず、削除もされないゴミになる)
        try:
            client.delete_object(ns, bucket, object_name)
        except Exception:
            logger.exception("orphan video cleanup failed (ignored)")
        raise

    captured = stored["captured_at"]
    return {
        "id": asset_id, "title": stored["title"], "collection": stored["collection"],
        "category": stored["category"], "rights": stored["rights"],
        # 一覧・詳細と同じ表記(UTC + "Z")で返す
        "captured_at": captured.strftime("%Y-%m-%dT%H:%M:%SZ") if captured else None,
        "duration_ms": duration_ms, "object_name": object_name,
        "bytes": size, "analysis_state": "pending", "vision_state": None,
    }


# --- 直接アップロード(VID-07 / specs/20 §2) -----------------------------------


def _par_url(par: Any) -> str:
    """PAR の公開 URL。トークンは**作成時の応答にしか入っていない**(後から引けない)。"""
    region = get_settings().oci_region
    return getattr(par, "full_path", None) or (
        f"https://objectstorage.{region}.oraclecloud.com{par.access_uri}"
    )


def create_upload_url(
    owner: str,
    filename: str,
    size_bytes: int,
    *,
    title: str | None = None,
    collection: str | None = None,
    category: str | None = None,
    rights: str | None = None,
    captured_at: datetime | None = None,
    duration_ms: int | None = None,
    ttl_seconds: int = UPLOAD_TTL_SECONDS,
) -> dict[str, Any]:
    """本体を受け取らずに登録を始める —— 台帳に `uploading` の行を作り、
    **書き込み専用・短命・オブジェクト名を固定した** PAR を返す(VID-07)。

    ゲートウェイの本文上限は 20 MiB(`GATEWAY_MAX_BODY_BYTES`)で、4K の素材は入らない。
    **本体をゲートウェイに通さない**ことが目的なので、ブラウザはこの PAR へ直接 PUT し、
    上げ終えたら `complete_upload` で確定する。

    PAR は次の 3 つを同時に満たす(tasks/VID-07 禁止事項「読み書き両用・長命にしない」):

    - `ObjectWrite` = **書き込み専用**。実測でこの PAR からは GET / HEAD とも 404 になる
      (`runs/.../e2e/spike-par-cors.md`)。読める PAR を配ると、上げる権利が読む権利になる
    - `object_name` を**この映像 1 個に固定**する。`AnyObjectWrite` だとバケット全体に
      書ける鍵を配ることになる
    - `time_expires` は既定 1 時間(`UPLOAD_TTL_SECONDS`)

    **PAR を先に作り、台帳の行は後**。逆順にすると、PAR の発行に失敗したときに
    上げようのない行が残る。この順なら失敗時に PAR を消せばよく、消し損ねても
    そこへ書ける相手は誰も居ない(URL を返していない)。

    **ここで自分の古い中断分を回収する**(review-2 VID07-006)。回収を分析の前段だけに
    置くと、**一度も分析しない利用者の中断分は永久に残る** —— 途中まで上がった大きな
    本体と、上げようのない行が積む。登録をやり直す人はここを必ず通るので、
    中断した本人が次に登録した時点で片付く。回収の失敗で新しい登録は止めない。
    """
    if not 0 < ttl_seconds <= UPLOAD_TTL_SECONDS:
        raise ValueError(f"ttl_seconds must be in 1..{UPLOAD_TTL_SECONDS}")
    if not 0 < size_bytes <= MAX_BYTES:
        raise ValueError(f"size_bytes must be in 1..{MAX_BYTES}")

    import oci.object_storage.models as osm

    # 中断分の回収を**登録の入口**でも回す(分析だけを頼りにしない)。
    # 掃除が転んでも新しい登録は通す —— 片付けの失敗で登録できなくなるほうが実害が大きい
    try:
        reap_stale_uploads(owner)
    except Exception:
        logger.exception("stale upload reap failed (ignored)")

    bucket = require_bucket()
    client = os_client()
    ns = client.get_namespace().data

    asset_id = str(uuid.uuid4())
    ext = pathlib.Path(filename).suffix.lower()
    # キーに利用者の入力(ファイル名)を混ぜないのは create_asset と同じ。以降の操作は
    # **台帳に記録した object_name** だけを使い、組み立て直さない
    object_name = f"video/{owner}/{asset_id}/source{ext}"
    content_type = content_type_for(filename)
    expires = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    par = client.create_preauthenticated_request(
        ns, bucket,
        osm.CreatePreauthenticatedRequestDetails(
            name=f"{_UPLOAD_PAR_NAME_PREFIX}{asset_id}",
            object_name=object_name,
            access_type="ObjectWrite",
            time_expires=expires,
        ),
    ).data

    # 正規化は 1 回だけ(create_asset と同じ理由。保存する値と返す値を別々に作らない)
    stored = {
        "title": _fit(title or filename, 500),
        "collection": _fit(collection, 255),
        "category": _fit(category, 255),
        "rights": _fit(rights, 1000),
        "captured_at": to_utc_naive(captured_at),
    }
    try:
        with connect() as conn:
            conn.cursor().execute(
                """
                INSERT INTO video_assets(id, owner_sub, title, collection, category,
                                         rights, captured_at, duration_ms, object_name,
                                         analysis_state, upload_state)
                VALUES (:id, :o, :t, :coll, :cat, :rights, :captured, :dur, :obj,
                        'pending', 'uploading')
                """,
                id=asset_id, o=owner, t=stored["title"], coll=stored["collection"],
                cat=stored["category"], rights=stored["rights"],
                captured=stored["captured_at"], dur=duration_ms, obj=object_name,
            )
            conn.commit()
    except Exception:
        # 誰も使えない書き込み口を残さない
        try:
            client.delete_preauthenticated_request(ns, bucket, par.id)
        except Exception:
            logger.exception("upload PAR cleanup failed (ignored)")
        raise

    return {
        "id": asset_id,
        "upload_url": _par_url(par),
        "object_name": object_name,
        # ブラウザは**この値をそのまま** PUT の Content-Type に付ける。complete 側は
        # 同じ規則で計算し直した値と突き合わせるので、別の型を混ぜると弾かれる
        "content_type": content_type,
        "expires_at": expires.isoformat(),
        "max_bytes": MAX_BYTES,
        "upload_state": "uploading",
    }


def _abandon_upload(owner: str, asset_id: str, object_name: str) -> bool:
    """確定しなかった登録を**台帳の行・オブジェクト・PAR まで**片付ける。片付けたかを返す。

    **行を先に消し、本体は後**。順番が逆だと、`uploading` の行を消せなかった相手
    (= 別の実行が先に `ready` にした)の**確定済みの本体を消してしまう**: 回収側が
    古い行を拾った直後に complete が通ると、本体を消してから DELETE が 0 件になり、
    「台帳にはあるが本体が無い映像」が残る。条件付き DELETE を**権利の取得**として使い、
    取れたときだけ本体に触る。

    取れなかったときは何もしない —— その登録は誰か(確定した側 / 利用者の削除)が
    既に引き取っている。取った後の掃除に失敗した場合は本体だけが残るが、台帳に行の
    無いオブジェクトは `video_frames.reap_orphan_assets` が後から回収する。
    """
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM video_assets"
            " WHERE id = :id AND owner_sub = :o AND upload_state = 'uploading'",
            id=asset_id, o=owner,
        )
        claimed = cur.rowcount > 0
        conn.commit()
    if claimed:
        _purge_objects(object_name)
    return claimed


def complete_upload(owner: str, asset_id: str) -> dict[str, Any]:
    """上げ終えた本体を**実物で検証してから**確定する(VID-07)。

    **「入れたと言われたから入った」ことにしない。** ブラウザの PUT が 200 を返したと
    いう申告ではなく、Object Storage に問い合わせて (a) 在ること (b) 空でないこと
    (c) 上限を超えていないこと (d) Content-Type が発行時に渡した値と一致することを見る。
    落ちたらオブジェクトごと片付けて `UploadVerificationError`(API は 422)。

    **見る前に書き込み口を閉じる。** アップロード用 PAR は検証の**前**に消す ——
    後に回すと、`head_object` で確かめてから台帳を `ready` にするまでの隙間に、
    まだ生きている同じ PAR で中身を差し替えられる(検証した実物と、確定した実物が
    別のものになる)。順番を逆にしただけで検証は意味を失うので、ここは
    `strict=True` で**消せなかったら進まない**。

    既に `ready` の行に対しては**何もせず返す**。complete の再送(応答が届かずに
    もう一度呼ぶ)は正常な経路で、409 にすると成功した登録が失敗に見える。
    """
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT object_name, upload_state FROM video_assets"
            " WHERE id = :id AND owner_sub = :o",
            id=asset_id, o=owner,
        )
        row = cur.fetchone()
    if row is None:
        raise LookupError(asset_id)
    object_name, upload_state = row
    if upload_state != "uploading":
        return get_asset(owner, asset_id) or {}

    bucket = require_bucket()
    client = os_client()
    ns = client.get_namespace().data
    prefix = object_name.rsplit("/", 1)[0] + "/"

    import oci

    # **検証の前に書き込み口を閉じる**(上の docstring)。ここで失敗したら進まない ——
    # 閉じられないまま検証しても「確かめた実物が確定される」保証にならない
    _delete_pars(client, ns, bucket, prefix, strict=True)

    try:
        headers = client.head_object(ns, bucket, object_name).headers
    except oci.exceptions.ServiceError as e:
        if e.status != 404:
            raise
        headers = None

    if headers is None:
        _abandon_upload(owner, asset_id, object_name)
        raise UploadVerificationError(
            "アップロードされた映像が見つかりません。もう一度登録してください"
        )

    size = int(headers.get("content-length") or 0)
    # `video/mp4; charset=...` のような付帯を落として比べる
    got = (headers.get("content-type") or "").split(";")[0].strip().lower()
    expected = content_type_for(object_name)
    problem = None
    if size <= 0:
        problem = "アップロードされた映像が空です(0 バイト)"
    elif size > MAX_BYTES:
        problem = (
            f"映像が大きすぎます({size} バイト。上限 {MAX_BYTES} バイト)"
        )
    elif got != expected.lower():
        problem = (
            f"アップロードされた映像の種別が違います"
            f"(受け取った値 '{got}' / 期待 '{expected}')"
        )
    if problem:
        _abandon_upload(owner, asset_id, object_name)
        raise UploadVerificationError(problem)

    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE video_assets SET upload_state = 'ready'"
            " WHERE id = :id AND owner_sub = :o AND upload_state = 'uploading'",
            id=asset_id, o=owner,
        )
        confirmed = cur.rowcount > 0
        conn.commit()
    if not confirmed:
        # 更新できなかった理由は 2 つある。**取り違えると、確定したばかりの映像の
        # 本体を消す。** complete が同時に 2 回呼ばれると(二度押し・応答が届かずの
        # 再送)、負けた側から見た行は既に `ready` —— ここで一律に片付けると、
        # 勝った側が確定した本体を消してしまう。
        with connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT upload_state FROM video_assets WHERE id = :id AND owner_sub = :o",
                id=asset_id, o=owner,
            )
            row = cur.fetchone()
        if row is not None and row[0] == "ready":
            asset = get_asset(owner, asset_id) or {}
            asset["bytes"] = size
            return asset
        # 行そのものが消えていた(回収された / 利用者が削除した)。**上げた本体を
        # 残さない** —— 台帳から辿れないオブジェクトは誰にも消せなくなる
        _purge_objects(object_name)
        raise LookupError(asset_id)

    asset = get_asset(owner, asset_id) or {}
    asset["bytes"] = size
    return asset


def reap_stale_uploads(owner: str) -> list[str]:
    """`uploading` のまま放置された登録を回収する(tasks/VID-07)。

    ブラウザが PUT の途中で閉じられると、行は `uploading`・本体は途中まで、という
    残骸になる。**握りつぶすのとは違う**(specs/20 §3) —— 起きないことにするのではなく、
    ここで後から回収すると決める。`video_frames.reap_orphan_assets` から呼ばれる。

    消すのは `UPLOAD_STALE_SECONDS`(PAR の寿命の 2 倍)より古い行だけ。**まだ有効な
    PAR で上げている最中の行を消さない** —— 消すと、上げ切った直後の complete が
    「見つかりません」になり、利用者から見れば理由もなく登録が消える。
    """
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(
        seconds=UPLOAD_STALE_SECONDS
    )
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, object_name FROM video_assets"
            " WHERE owner_sub = :o AND upload_state = 'uploading'"
            "   AND created_at < :cut",
            o=owner, cut=cutoff,
        )
        rows = cur.fetchall()

    reclaimed: list[str] = []
    for asset_id, object_name in rows:
        try:
            claimed = _abandon_upload(owner, asset_id, object_name)
        except Exception:
            # 1 本の失敗で残りを止めない。次の回収でもう一度拾う
            logger.exception("stale upload cleanup failed: %s", asset_id)
            continue
        # 取れなかった行は**この回収が片付けたものではない**(拾った後に確定された /
        # 利用者が消した)。数に入れると、触っていないものを片付けたと記録することになる
        if claimed:
            reclaimed.append(asset_id)
    if reclaimed:
        logger.info("reaped %d stale video upload(s) for %s", len(reclaimed), owner)
    return reclaimed


# --- 一覧・詳細 ---------------------------------------------------------------


def list_assets(owner: str, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    limit = max(1, min(limit, LIST_LIMIT_MAX))
    offset = max(0, offset)
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            # **確定していない登録は出さない**(VID-07)。本体がまだ無い行を一覧に
            # 混ぜると、開いても再生できず分析もできない映像が並ぶ。放置された行は
            # `reap_stale_uploads` が回収する
            f"SELECT {_ASSET_COLUMNS} FROM video_assets"
            " WHERE owner_sub = :o AND upload_state = 'ready'"
            " ORDER BY created_at DESC, id DESC"
            " OFFSET :off ROWS FETCH NEXT :lim ROWS ONLY",
            o=owner, off=offset, lim=limit,
        )
        return [_row_to_asset(r) for r in cur.fetchall()]


def get_asset(owner: str, asset_id: str) -> dict[str, Any] | None:
    """詳細(場面つき)。場面を埋めるのは後続の分析タスクで、v1 の登録直後は空。

    **確定前(`uploading`)の行もそのまま返す。** 一覧(`list_assets`)は隠すが、ここは
    隠さない —— id を知っているのは自分だけで、隠すと「登録したのに詳細が 404」に
    なって状況が読めなくなる。本体を要る操作(再生・分析)はそれぞれの側で止めている。
    """
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT {_ASSET_COLUMNS} FROM video_assets"
            " WHERE id = :id AND owner_sub = :o",
            id=asset_id, o=owner,
        )
        row = cur.fetchone()
        if not row:
            return None
        asset = _row_to_asset(row)
        cur.execute(
            f"SELECT {_SCENE_COLUMNS} FROM video_scenes"
            " WHERE asset_id = :id ORDER BY start_ms",
            id=asset_id,
        )
        asset["scenes"] = [row_to_scene(r) for r in cur.fetchall()]
        return asset


def object_name_for(
    owner: str, asset_id: str, *, ready_only: bool = False
) -> str | None:
    """台帳が持つ本体の位置。

    `ready_only` は「本体が確定している行だけ」(VID-07)。再生のように**本体が要る**
    操作で使う —— まだ上がっていないオブジェクトへ PAR を発行すると、期限切れまで
    404 を返す URL を配ることになる。削除は逆に `uploading` の行も対象にする
    (登録を途中でやめた利用者が消せなくなる)。
    """
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT object_name FROM video_assets WHERE id = :id AND owner_sub = :o"
            "   AND (:ready = 0 OR upload_state = 'ready')",
            id=asset_id, o=owner, ready=1 if ready_only else 0,
        )
        row = cur.fetchone()
        return row[0] if row else None


# --- 分析の入口(排他) ---------------------------------------------------------


def claim_analysis(owner: str, asset_id: str) -> str:
    """その映像の分析を開始する権利を取り、**その権利の印**を返す(specs/20 §3)。

    **アトミックな 1 文の UPDATE が入口**。`analysis_state` を条件に含めることで、
    同じ映像に対する分析が同時に 2 つ走らない。読んでから書く 2 段にすると、
    その隙間に相手も同じ判定を通れてしまう。

    取れなければ `AnalysisInProgressError`(API は 409)。映像が無い/他人のものなら
    `LookupError`(所有者以外に id の存在有無を漏らさない)。

    `ANALYSIS_STALE_SECONDS` を過ぎた `running` は引き継ぐ —— 分析中に落ちた映像を
    `running` のまま固めない。ただし**引き継ぎは「落ちている」ことを保証しない**
    (単に遅いだけかもしれない)。そこで取るたびに新しい `analysis_token` を書き、
    それを**権利の印**として返す。以降の書き込み(`_save_scenes` / `finish_analysis`)は
    この印が台帳の値と一致するときだけ通す。印が変わっていれば、その実行は
    引き継がれた側なので何も書かずに降りる。

    `analysis_error` は消す(前回の失敗理由を今回の結果と混ぜない)。
    """
    stale = datetime.now(UTC).replace(tzinfo=None) - timedelta(
        seconds=ANALYSIS_STALE_SECONDS
    )
    token = uuid.uuid4().hex
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE video_assets
               SET analysis_state = 'running',
                   analysis_started_at = SYS_EXTRACT_UTC(SYSTIMESTAMP),
                   analysis_token = :tok,
                   analysis_error = NULL
             WHERE id = :id AND owner_sub = :o
               AND upload_state = 'ready'
               AND (analysis_state <> 'running'
                    OR analysis_started_at IS NULL
                    OR analysis_started_at < :stale)
            """,
            id=asset_id, o=owner, stale=stale, tok=token,
        )
        claimed = cur.rowcount > 0
        conn.commit()
        if claimed:
            return token
        # 取れなかった理由を分ける。「走っている」「本体がまだ無い」「無い」を同じ
        # 扱いにすると、利用者はどれなのか判らない
        cur.execute(
            "SELECT upload_state FROM video_assets WHERE id = :id AND owner_sub = :o",
            id=asset_id, o=owner,
        )
        row = cur.fetchone()
        if row is None:
            raise LookupError(asset_id)
        if row[0] != "ready":
            raise UploadIncompleteError(asset_id)
    raise AnalysisInProgressError(asset_id)


def finish_analysis(
    owner: str, asset_id: str, state: str, error: str | None = None,
    *, token: str | None = None,
) -> bool:
    """分析の終わりを台帳へ書き、`running` を解く。書けたかどうかを返す。

    **`token` を渡したときは、その権利がまだ自分のものである場合だけ書く。**
    引き継がれた古い実行がここを素通しで書くと、新しい実行の `running` を解いて
    3 本目の開始まで許してしまう。

    **失敗を握りつぶさない**(specs/20 §3)。`failed` / `partial` は理由を必ず伴い、
    伴わない呼び出しは `ValueError` で**書く前に**弾く。約束を docstring だけに書くと、
    理由の無い `failed` がそのまま保存できてしまう(VID-02 レビュー指摘)。この関数の
    主な利用者は分析の実行系(`video_frames` / `video_analyze`)で、そこは例外処理の
    途中から呼ぶ —— 理由が空になる経路を作りやすいので、入口で落とす。
    """
    if state not in ANALYSIS_STATES:
        raise ValueError(
            f"unknown analysis_state {state!r} (allowed: {sorted(ANALYSIS_STATES)})"
        )
    # **空白だけの理由を「理由あり」と数えない。** 空文字と同じく、読んだ人には
    # 何も伝わらない。列幅に収めるのは判定の後ではなく前(判定は保存する値に対して行う)。
    # 収めるのは**バイト長**(`_fit_bytes` の docstring)
    reason = _fit_bytes(error.strip() if error else error, ANALYSIS_ERROR_MAX_BYTES)
    if state in ANALYSIS_STATES_WITH_REASON and reason is None:
        raise ValueError(f"analysis_state {state!r} requires a reason")
    if state not in ANALYSIS_STATES_WITH_REASON and reason is not None:
        raise ValueError(f"analysis_state {state!r} must not carry a reason")

    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE video_assets SET analysis_state = :s, analysis_error = :e"
            " WHERE id = :id AND owner_sub = :o"
            "   AND (:tok IS NULL OR analysis_token = :tok)",
            s=state, e=reason, id=asset_id, o=owner, tok=token,
        )
        conn.commit()
        return cur.rowcount > 0


# --- 削除 ---------------------------------------------------------------------


def delete_asset(owner: str, asset_id: str) -> bool:
    """映像・サムネイル・場面をまとめて削除する(specs/20 §2)。

    **本体を先に消し、台帳は後**。逆順にすると Object Storage 側の削除が落ちたときに
    誰からも辿れない本体が残る(課金され続け、次の削除でも消せない)。この順なら
    失敗時は台帳行が残るだけで、もう一度 DELETE すれば片付く。

    **分析の実行中に削除された場合の即時整合は取らない**(specs/20 §3「同時実行の範囲」)。
    掃除の後に分析がサムネイルを置けば残骸になるが、データは壊れず、
    `video_frames.reap_orphan_assets` が後から回収する。ここを完全に閉じるには
    Object Storage と DB をまたぐ分散トランザクションか映像ごとの外部ロックが要り、
    実害(残骸オブジェクト数個)に対して仕組みが重すぎる、と決めた。
    """
    object_name = object_name_for(owner, asset_id)
    if object_name is None:
        return False

    _purge_objects(object_name)

    with connect() as conn:
        cur = conn.cursor()
        # 場面(video_scenes)と修正履歴は FK の ON DELETE CASCADE で一緒に消える
        cur.execute(
            "DELETE FROM video_assets WHERE id = :id AND owner_sub = :o",
            id=asset_id, o=owner,
        )
        conn.commit()
        return cur.rowcount > 0


def _purge_objects(object_name: str) -> None:
    """その映像に属するオブジェクトと、発行済み PAR を消す。

    サムネイルは `video/<owner>/<id>/thumb/...` に増える(後続の分析タスク)ので、
    本体 1 個ではなく**プレフィックス配下を全部**消す。PAR を残すと、台帳から
    消えた後もその URL で映像を取れてしまう —— 削除したのに読める状態を残さない。
    """
    bucket = require_bucket()
    client = os_client()
    ns = client.get_namespace().data
    prefix = object_name.rsplit("/", 1)[0] + "/"

    start = None
    while True:
        page = client.list_objects(ns, bucket, prefix=prefix, fields="name", start=start).data
        for obj in page.objects:
            client.delete_object(ns, bucket, obj.name)
        start = getattr(page, "next_start_with", None)
        if not start:
            break

    _delete_pars(client, ns, bucket, prefix)


def _delete_pars(client, ns: str, bucket: str, prefix: str, *, strict: bool = False) -> None:
    """そのプレフィックス配下を指す PAR を全部消す。

    **全ページ集めてから消す。** 再生要求のたびに PAR が増えるので 1 ページ目だけでは
    足りず、かつページ位置は件数に依存するため、辿りながら消すと詰めた分が飛ばされて
    消し残る(= 削除後もその URL で読める)。オブジェクト側の next_start_with は
    名前カーソルなので、この問題は起きない。

    `strict` は**消せたことが前提になる呼び出し**用(`complete_upload` の入口)。
    そこでは「書き込み口を閉じてから実物を見る」ことが検証の意味そのものなので、
    消せなかったのに続けると、見た後で中身を差し替えられる余地を残す。
    """
    try:
        targets = []
        page = None
        while True:
            resp = client.list_preauthenticated_requests(
                ns, bucket, object_name_prefix=prefix, page=page
            )
            targets.extend(resp.data)
            page = getattr(resp, "next_page", None)
            if not page:
                break
        for par in targets:
            try:
                client.delete_preauthenticated_request(ns, bucket, par.id)
            except Exception as e:
                # **既に閉じられていた(404)は、閉じられたのと同じ。** 同じ映像の
                # complete が同時に走ると、両方が同じ PAR を数えてから消しにいく。
                # 後から消したほうを失敗にすると、登録は成功しているのに 500 を返す
                # ことになり、「再送は成功として返す」という約束が破れる
                if getattr(e, "status", None) != 404:
                    raise
    except Exception:
        if strict:
            raise
        # 本体が消えていれば PAR は 404 を返すだけになる。掃除漏れで削除全体を
        # 失敗にはしないが、黙って握り潰さない
        logger.exception("video PAR cleanup failed (ignored)")


# --- 再生 URL(PAR) ------------------------------------------------------------


def playback_url(
    owner: str, asset_id: str, ttl_seconds: int = PLAYBACK_TTL_SECONDS
) -> dict[str, Any] | None:
    """期限付きの再生 URL を発行する(specs/20 §2)。

    PAR のトークンは**作成時の応答にしか入っていない**(後から引き直せない)ので、
    再生要求のたびに発行する。寿命を短く保ち、映像の削除時に `_purge_objects` が
    まとめて消す。
    """
    if not 0 < ttl_seconds <= PLAYBACK_TTL_MAX:
        raise ValueError(f"ttl_seconds must be in 1..{PLAYBACK_TTL_MAX}")
    object_name = object_name_for(owner, asset_id, ready_only=True)
    if object_name is None:
        return None

    import oci.object_storage.models as osm

    bucket = require_bucket()
    client = os_client()
    ns = client.get_namespace().data
    expires = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    par = client.create_preauthenticated_request(
        ns, bucket,
        osm.CreatePreauthenticatedRequestDetails(
            name=f"{_PAR_NAME_PREFIX}{asset_id}",
            object_name=object_name,
            access_type="ObjectRead",
            time_expires=expires,
        ),
    ).data
    return {"url": _par_url(par), "expires_at": expires.isoformat()}
