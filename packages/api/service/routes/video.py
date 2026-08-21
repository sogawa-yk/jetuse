"""映像の登録・一覧・詳細・削除・再生 URL / 分析 / 場面の横断検索 / 場面メタデータの確認・修正
(VID-01 / VID-03 / VID-04 / VID-05 / specs/20 §2 §3 §4 §5)。
"""

import asyncio
import logging
import pathlib
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile

from jetuse_core import video as video_repo
from jetuse_core import video_analyze, video_edit, video_frames, video_search
from jetuse_core.auth import AuthContext, require_user

from ..deps import require_video
from ..schemas import VideoSceneEdit, VideoSearchRequest, VideoUploadUrlRequest

logger = logging.getLogger("jetuse.service")
router = APIRouter()

_ALLOWED_HINT = "/".join(sorted(e.lstrip(".") for e in video_repo.ALLOWED_EXTENSIONS))


def _MB(n: int) -> str:
    """バイト数を利用者に見せる形へ。**桁を数えさせない**(20971520 では伝わらない)。"""
    return f"{n / (1024 * 1024):.0f}MB"


def _check_extension(filename: str) -> str:
    """拡張子を検査し、正規化したファイル名を返す。multipart 経路と直接アップロードで
    **同じ規則**を使う —— 片方だけ緩いと、そこから他方が想定しない映像が入る。
    """
    name = pathlib.Path(filename or "untitled").name
    ext = pathlib.Path(name).suffix.lower()
    if ext not in video_repo.ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=422,
            detail=f"unsupported video type '{ext}'. allowed: {_ALLOWED_HINT}",
        )
    return name


def _parse_captured_at(raw: str | None) -> datetime | None:
    """撮影日時。**読めない値を黙って NULL にしない**(specs/20 §1)。

    NULL は「不明」を表す値であって、入力ミスの受け皿ではない。読めなければ 422 で
    返し、利用者に直させる。オフセット付き("Z" / "+09:00")は UTC へ寄せる
    (保存先が TIMESTAMP = タイムゾーン無しのため。`video.to_utc_naive`)。
    """
    if raw is None or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError as e:
        raise HTTPException(
            status_code=422, detail=f"captured_at must be ISO-8601: {raw[:40]}"
        ) from e
    return video_repo.to_utc_naive(parsed)


def _parse_duration_ms(raw: str | None) -> int | None:
    if raw is None or not raw.strip():
        return None
    try:
        value = int(raw)
    except ValueError as e:
        raise HTTPException(status_code=422, detail="duration_ms must be an integer") from e
    if value < 0:
        raise HTTPException(status_code=422, detail="duration_ms must not be negative")
    return value


async def _check_upload(file: UploadFile) -> tuple[str, int]:
    """拡張子・サイズ・空を検査し、本文は**読まずに**名前とサイズだけ返す。

    本文はストリームのまま Object Storage へ渡す(映像 1 本をメモリに載せない)。
    """
    name = _check_extension(file.filename or "untitled")
    size = file.size
    if size is None:  # 稀に size を持たないクライアント。末尾へ seek して測る
        size = await asyncio.to_thread(file.file.seek, 0, 2)
        await asyncio.to_thread(file.file.seek, 0)
    if size > video_repo.MULTIPART_MAX_BYTES:
        # **実態と違う案内をしない**(tasks/VID-07 禁止事項)。この経路は API Gateway の
        # 本文上限(20 MiB・2026-08-20 実測)に阻まれるので、アプリ側の 500MB には
        # そもそも届かない。ここを 500MB のままにすると、画面は「500MB まで」と言い
        # ながら 20MB でゲートウェイに 413 で切られ、利用者は理由を辿れない。
        # **直せる道を必ず添える** —— 大きい映像は本文を通さない経路で登録できる
        # **丸めた値だけを出さない。** 20,905,985 バイトを「20MB」と丸めると
        # 「20MB までです(送られたファイルは 20MB)」という、読んでも判らない文になる
        # (実測でそう出た)。境界の話をするときは実数を添える
        raise HTTPException(
            status_code=413,
            detail=(
                f"この経路は API Gateway の本文上限のため約 {_MB(video_repo.MULTIPART_MAX_BYTES)}"
                f"({video_repo.MULTIPART_MAX_BYTES:,} バイト)までです"
                f"(送られたファイルは {size:,} バイト)。"
                f"大きい映像は /videos の登録画面から Object Storage へ直接アップロード"
                f"してください(最大 {_MB(video_repo.MAX_BYTES)})"
            ),
        )
    if not size:
        raise HTTPException(status_code=422, detail="empty file")
    return name, size


@router.post("/api/video/assets")
async def create_video_asset(
    file: UploadFile,
    user: Annotated[AuthContext, Depends(require_user)],
    _: Annotated[None, Depends(require_video)],
    title: Annotated[str | None, Form()] = None,
    collection: Annotated[str | None, Form()] = None,
    category: Annotated[str | None, Form()] = None,
    rights: Annotated[str | None, Form()] = None,
    captured_at: Annotated[str | None, Form()] = None,
    duration_ms: Annotated[str | None, Form()] = None,
):
    """1 リクエスト 1 本。複数件は画面側が順に投げる(specs/20 §2)。"""
    captured = _parse_captured_at(captured_at)
    duration = _parse_duration_ms(duration_ms)
    name, size = await _check_upload(file)
    await asyncio.to_thread(file.file.seek, 0)
    return await asyncio.to_thread(
        lambda: video_repo.create_asset(
            user.subject, name, file.file, size,
            title=title, collection=collection, category=category, rights=rights,
            captured_at=captured, duration_ms=duration,
        )
    )


@router.post("/api/video/assets/upload-url")
async def create_video_upload_url(
    req: VideoUploadUrlRequest,
    user: Annotated[AuthContext, Depends(require_user)],
    _: Annotated[None, Depends(require_video)],
):
    """登録の入口(1/2)。**本体を受け取らずに**書き込み専用の PAR を返す(VID-07)。

    ゲートウェイの本文上限は 20 MiB(`video.GATEWAY_MAX_BODY_BYTES`・実測)なので、
    4K の素材は multipart 経路では入らない。ブラウザはここで貰った URL へ直接 PUT し、
    上げ終えたら `complete` を呼ぶ。**本体はゲートウェイを通らない。**

    上限超過はここで弾く(413)。PAR を配ってから落とすと、利用者は 500MB を上げ切った
    後で失敗を知ることになる。
    """
    name = _check_extension(req.filename)
    if req.size_bytes > video_repo.MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"映像が大きすぎます(送られたファイルは {req.size_bytes:,} バイト)。"
                f"上限は {_MB(video_repo.MAX_BYTES)}({video_repo.MAX_BYTES:,} バイト)です"
            ),
        )
    captured = _parse_captured_at(req.captured_at)
    return await asyncio.to_thread(
        lambda: video_repo.create_upload_url(
            user.subject, name, req.size_bytes,
            title=req.title, collection=req.collection, category=req.category,
            rights=req.rights, captured_at=captured, duration_ms=req.duration_ms,
        )
    )


@router.post("/api/video/assets/{asset_id}/complete")
async def complete_video_upload(
    asset_id: str,
    user: Annotated[AuthContext, Depends(require_user)],
    _: Annotated[None, Depends(require_video)],
):
    """登録の入口(2/2)。上げ終えた本体を**実物で検証してから**確定する(VID-07)。

    **ブラウザの申告を信じない。** Object Storage に問い合わせて、在ること・空でない
    こと・上限内であること・Content-Type が発行時の値と一致することを見る。落ちたら
    オブジェクト・PAR・台帳の行をまとめて片付けて 422 —— 中途半端な登録を残さない。

    再送(応答が届かずにもう一度呼ぶ)は成功として返す。確定済みの行に 409 を返すと、
    通信の揺れが「登録に失敗した」に見える。
    """
    try:
        return await asyncio.to_thread(video_repo.complete_upload, user.subject, asset_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail="video asset not found") from e
    except video_repo.UploadVerificationError as e:
        # **直せる失敗**。理由をそのまま見せる(「失敗しました」では上げ直しようがない)
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.get("/api/video/assets")
async def list_video_assets(
    user: Annotated[AuthContext, Depends(require_user)],
    _: Annotated[None, Depends(require_video)],
    limit: int = 50,
    offset: int = 0,
):
    assets = await asyncio.to_thread(video_repo.list_assets, user.subject, limit, offset)
    return {"assets": assets}


@router.get("/api/video/assets/{asset_id}")
async def get_video_asset(
    asset_id: str,
    user: Annotated[AuthContext, Depends(require_user)],
    _: Annotated[None, Depends(require_video)],
):
    asset = await asyncio.to_thread(video_repo.get_asset, user.subject, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="video asset not found")
    return asset


@router.get("/api/video/assets/{asset_id}/playback")
async def get_video_playback(
    asset_id: str,
    user: Annotated[AuthContext, Depends(require_user)],
    _: Annotated[None, Depends(require_video)],
):
    """期限付きの再生 URL(PAR)。API は映像を中継しない。"""
    res = await asyncio.to_thread(video_repo.playback_url, user.subject, asset_id)
    if res is None:
        raise HTTPException(status_code=404, detail="video asset not found")
    return res


@router.post("/api/video/assets/{asset_id}/analyze")
async def analyze_video_asset(
    asset_id: str,
    user: Annotated[AuthContext, Depends(require_user)],
    _: Annotated[None, Depends(require_video)],
):
    """映像を分析する。**再分析も同じ入口**(specs/20 §3 / 要求8)。

    同じ映像の分析が既に走っていれば 409(specs/20 §3「同時実行の範囲」)。入口の排他は
    `video.claim_analysis` のアトミックな UPDATE が持っており、ここでは例外を
    HTTP に写すだけ —— 判定をルータ側で二重に持つと、片方だけ直したときに壊れる。

    **同期で返す。** v1 は短い映像で成立させると決めてある(ADR-0032「v1 の外」:
    長時間映像・大量映像の一括処理)。ジョブ基盤を足すのはその範囲を広げるときで、
    いまは場面数の上限(`video_analyze.MAX_SCENES`)で所要時間を抑え、超過分は
    `partial` の理由として残す。
    """
    try:
        return await asyncio.to_thread(video_analyze.analyze_asset, user.subject, asset_id)
    except video_repo.AnalysisInProgressError as e:
        raise HTTPException(
            status_code=409, detail="この映像の分析はすでに実行中です"
        ) from e
    except video_repo.UploadIncompleteError as e:
        # **本体がまだ無い**(2 段アップロードの途中)。404 にすると「無い」と読めるが、
        # 行は在るし上げ直せば使える。分析できないのは映像の中身の問題ではない
        raise HTTPException(
            status_code=409,
            detail="この映像はアップロードが完了していません。登録し直してください",
        ) from e
    except (LookupError, video_repo.AnalysisSupersededError) as e:
        # 引き継がれた側は「自分の結果は無い」。**別の実行の結果を自分のものとして
        # 返さない** —— 409 で「もう一度取り直せ」と伝える
        if isinstance(e, video_repo.AnalysisSupersededError):
            raise HTTPException(
                status_code=409, detail="この映像の分析は別の実行に引き継がれました"
            ) from e
        raise HTTPException(status_code=404, detail="video asset not found") from e
    except video_frames.FfmpegUnavailableError as e:
        # **配備の不備を「入力が悪い」に見せない。** ffmpeg が起動できないのは
        # サーバ側の問題で、利用者が映像を差し替えても直らない
        raise HTTPException(
            status_code=503, detail=f"映像処理の依存が使えません: {e}"
        ) from e
    except video_analyze.VisionServiceError as e:
        # 視覚 LLM を呼べなかった(認証・429・タイムアウト・サービス障害)。上流の障害な
        # ので 502 —— 422 にすると、直せないものを利用者に直させようとすることになる
        raise HTTPException(status_code=502, detail=str(e)) from e
    except (video_analyze.VideoAnalyzeError, video_frames.VideoFrameError) as e:
        # ここまで来るのは映像そのものか応答の中身の問題。台帳には `failed` と理由が
        # 入っている(specs/20 §3「握りつぶさない」)。画面にも同じ理由を返す ——
        # 「失敗した」だけでは利用者が直せない
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.delete("/api/video/assets/{asset_id}")
async def delete_video_asset(
    asset_id: str,
    user: Annotated[AuthContext, Depends(require_user)],
    _: Annotated[None, Depends(require_video)],
):
    if not await asyncio.to_thread(video_repo.delete_asset, user.subject, asset_id):
        raise HTTPException(status_code=404, detail="video asset not found")
    return {"deleted": True}


@router.post("/api/video/search")
async def search_video_scenes(
    body: VideoSearchRequest,
    user: Annotated[AuthContext, Depends(require_user)],
    _: Annotated[None, Depends(require_video)],
):
    """場面を横断検索する(specs/20 §4 / 要求4・5・10・11)。

    **返すのは場面**。映像単位に丸めない(tasks/VID-04 禁止事項)。距離とメタデータ条件は
    同一の SQL で評価し(ADR-0032 決定4)、**しきい値では切らずに順位で返す**
    (実測で正解と無関係の差が 0.09 しかない。比較ドキュメント §5.5)。

    根拠(要求11)は `hits[].matched` に必ず入る —— なぜその場面が出たのかを利用者が
    確認できることは要件であり、AI 検索をブラックボックスにしないという方針そのもの。
    """
    filters = body.filters.model_dump(exclude_none=True) if body.filters else None
    try:
        return await asyncio.to_thread(
            video_search.search, user.subject,
            q=body.q, filters=filters,
            similar_to_scene_id=body.similar_to_scene_id, limit=body.limit,
        )
    except video_search.SearchInputError as e:
        # 条件の誤り(未知のキー・集合外の値・ベクトルの無い場面での類似検索)。
        # **黙って全件に落とさない**(絞り込めたつもりで別のものを見ることになる)
        raise HTTPException(status_code=422, detail=str(e)) from e
    except LookupError as e:
        # 類似検索の起点が無い / 他人のもの。所有者以外に id の存在有無を漏らさない
        raise HTTPException(status_code=404, detail="video scene not found") from e
    except video_search.SearchBackendError as e:
        # 検索語を埋め込めなかった(上流の障害)。利用者が検索語を変えても直らない
        raise HTTPException(status_code=502, detail=str(e)) from e

# --- 場面メタデータの確認・修正(VID-05 / specs/20 §5 / 要求8) -----------------


def _scene_http(fn, *args):
    """場面の編集系で共通の失敗の写し方。**理由ごとに違う番号を返す**。

    404 = 無い/他人のもの(所有者以外に存在を漏らさない)、409 = 分析中か、その場面が
    途中で作り直された(やり直せば通る)、422 = 送られた値が受け取れない(直せる)。
    ここを 1 か所に集めるのは、3 つのルートで対応付けがずれると
    「同じ失敗なのに画面の出方が違う」が起きるため。
    """
    try:
        return fn(*args)
    except LookupError as e:
        raise HTTPException(status_code=404, detail="video scene not found") from e
    except video_repo.AnalysisInProgressError as e:
        raise HTTPException(
            status_code=409,
            detail="この映像は分析中です。分析は場面を作り直すため、いまの修正は残りません",
        ) from e
    except video_edit.SceneChangedError as e:
        raise HTTPException(
            status_code=409,
            detail="この場面は編集中に変わりました。取り直してからやり直してください",
        ) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.patch("/api/video/scenes/{scene_id}")
async def patch_video_scene(
    scene_id: str,
    req: VideoSceneEdit,
    user: Annotated[AuthContext, Depends(require_user)],
    _: Annotated[None, Depends(require_video)],
):
    """場面のメタデータを直す。**`source` は `human` になり、埋め込みを作り直す**。

    送られた項目だけを直す(`exclude_unset`)。埋め込みを作り直せなかった場合も編集は
    保存し、`embedding_state = "failed"` と理由を返す —— 直した内容を上流の障害で
    捨てない。古いベクトルは残さない(直したのに古い説明で当たり続けることになる)。
    """
    changes = req.model_dump(exclude_unset=True)
    return await asyncio.to_thread(
        _scene_http, video_edit.patch_scene, user.subject, scene_id, changes
    )


@router.post("/api/video/scenes/{scene_id}/confirm")
async def confirm_video_scene(
    scene_id: str,
    user: Annotated[AuthContext, Depends(require_user)],
    _: Annotated[None, Depends(require_video)],
):
    """人が確認したことを残す(`ai` → `ai_confirmed`)。中身は変えない。"""
    return await asyncio.to_thread(
        _scene_http, video_edit.confirm_scene, user.subject, scene_id
    )


@router.get("/api/video/scenes/{scene_id}/edits")
async def list_video_scene_edits(
    scene_id: str,
    user: Annotated[AuthContext, Depends(require_user)],
    _: Annotated[None, Depends(require_video)],
    limit: int = 100,
):
    """その場面の修正履歴(何を誰がいつ)。**残した記録を読めるようにする。**"""
    edits = await asyncio.to_thread(
        _scene_http, video_edit.list_edits, user.subject, scene_id, limit
    )
    return {"edits": edits}


@router.delete("/api/video/scenes/{scene_id}")
async def delete_video_scene(
    scene_id: str,
    user: Annotated[AuthContext, Depends(require_user)],
    _: Annotated[None, Depends(require_video)],
):
    """不適切なメタデータを場面ごと消す(specs/20 §5)。履歴も一緒に消える。"""
    if not await asyncio.to_thread(
        _scene_http, video_edit.delete_scene, user.subject, scene_id
    ):
        raise HTTPException(status_code=404, detail="video scene not found")
    return {"deleted": True}
