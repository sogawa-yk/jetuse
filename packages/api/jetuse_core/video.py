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

# 再生 URL の既定寿命と天井。**無期限の PAR を作らない**。
# 天井を超える指定はクランプせず拒否する(黙って縮めると「延ばしたのに効かない」になる)
PLAYBACK_TTL_SECONDS = 3600
PLAYBACK_TTL_MAX = 24 * 3600

# 一覧のページング上限。天井超過はクランプする(一覧は取得件数が減るだけで意味が壊れない)
LIST_LIMIT_MAX = 100

_PAR_NAME_PREFIX = "jetuse-video-"

# 時刻列は **UTC で保存し、UTC として返す**(格納側は下記 to_utc_naive、既定値は
# migration の SYS_EXTRACT_UTC)。列は TIMESTAMP(タイムゾーン無し)なので、末尾の "Z" を
# 付けて「どの時間帯の値か」を明示する。付けないと "2026-08-19T10:00:00" が受け手の
# ローカル時刻と解釈されて 9 時間ずれる。**AT TIME ZONE は使わない** —— 素の TIMESTAMP に
# 掛けるとセッションの時間帯で解釈されてから変換され、UTC で入れた値が動く。
_TS = "TO_CHAR({col}, 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')"
_ASSET_COLUMNS = (
    "id, title, " + _TS.format(col="created_at") + ", duration_ms, "
    "collection, category, rights, " + _TS.format(col="captured_at") + ", "
    "analysis_state, analysis_error, vision_state, object_name, thumb_object, summary"
)


_SCENE_COLUMNS = (
    "id, start_ms, end_ms, description, tags, objects, people, actions, place, "
    "scene_kind, indoor, time_of_day, weather, screen_text, thumb_object, source, "
    + _TS.format(col="confirmed_at")
)


def to_utc_naive(value: datetime | None) -> datetime | None:
    """aware な日時は UTC へ寄せ、tzinfo を落として返す(naive はそのまま UTC 扱い)。

    保存先は `TIMESTAMP`(タイムゾーン無し)。オフセット付きの値をそのまま渡すと、
    同じ瞬間でも入力のオフセット次第で別の壁時計時刻として保存され、後の期間検索
    (specs/20 §4 の captured_from/to)が静かにずれる。**入口で 1 つの時間帯に寄せる。**
    """
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _os_client():
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


def _require_bucket() -> str:
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
    bucket = _require_bucket()
    client = _os_client()
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


# --- 一覧・詳細 ---------------------------------------------------------------


def list_assets(owner: str, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    limit = max(1, min(limit, LIST_LIMIT_MAX))
    offset = max(0, offset)
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT {_ASSET_COLUMNS} FROM video_assets WHERE owner_sub = :o"
            " ORDER BY created_at DESC, id DESC"
            " OFFSET :off ROWS FETCH NEXT :lim ROWS ONLY",
            o=owner, off=offset, lim=limit,
        )
        return [_row_to_asset(r) for r in cur.fetchall()]


def get_asset(owner: str, asset_id: str) -> dict[str, Any] | None:
    """詳細(場面つき)。場面を埋めるのは後続の分析タスクで、v1 の登録直後は空。"""
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
        asset["scenes"] = [
            {
                # JSON 列(tags/objects/people/actions)は**文字列のまま返す**。
                # 中身を作るのは後続の分析タスクで、ここで parse すると壊れた値を
                # 詳細 API 全体の 500 に変えてしまう(IS JSON で入口は守っている)
                "id": r[0], "start_ms": r[1], "end_ms": r[2], "description": r[3],
                "tags": r[4], "objects": r[5], "people": r[6], "actions": r[7],
                "place": r[8], "scene_kind": r[9], "indoor": r[10],
                "time_of_day": r[11], "weather": r[12], "screen_text": r[13],
                "thumb_object": r[14], "source": r[15], "confirmed_at": r[16],
            }
            for r in cur.fetchall()
        ]
        return asset


def _object_name(owner: str, asset_id: str) -> str | None:
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT object_name FROM video_assets WHERE id = :id AND owner_sub = :o",
            id=asset_id, o=owner,
        )
        row = cur.fetchone()
        return row[0] if row else None


# --- 削除 ---------------------------------------------------------------------


def delete_asset(owner: str, asset_id: str) -> bool:
    """映像・サムネイル・場面をまとめて削除する(specs/20 §2)。

    **本体を先に消し、台帳は後**。逆順にすると Object Storage 側の削除が落ちたときに
    誰からも辿れない本体が残る(課金され続け、次の削除でも消せない)。この順なら
    失敗時は台帳行が残るだけで、もう一度 DELETE すれば片付く。
    """
    object_name = _object_name(owner, asset_id)
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
    bucket = _require_bucket()
    client = _os_client()
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

    try:
        # **全ページ集めてから消す。** 再生要求のたびに PAR が増えるので 1 ページ目だけ
        # では足りず、かつページ位置は件数に依存するため、辿りながら消すと詰めた分が
        # 飛ばされて消し残る(= 削除後もその URL で読める)。
        # オブジェクト側の next_start_with は名前カーソルなので、この問題は起きない。
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
            client.delete_preauthenticated_request(ns, bucket, par.id)
    except Exception:
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
    object_name = _object_name(owner, asset_id)
    if object_name is None:
        return None

    import oci.object_storage.models as osm

    bucket = _require_bucket()
    client = _os_client()
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
    region = get_settings().oci_region
    url = getattr(par, "full_path", None) or (
        f"https://objectstorage.{region}.oraclecloud.com{par.access_uri}"
    )
    return {"url": url, "expires_at": expires.isoformat()}
