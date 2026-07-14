"""OCI Functionsルーター(ARCH-02): 非ストリーミングAPIの第1陣。

API GWのfunctions_routesから ORACLE_FUNCTIONS_BACKEND で呼ばれる。
担当セグメント: presets / dbchat / tts(計7エンドポイント)。
SSE・プロセス内状態・6MB超アップロードはCI側に残る(comparison/compute-architecture.md)。

ルーティング情報はFn-Http-*ヘッダ(API GW→Functions連携の標準)から取る。
共通ロジックはjetuse_coreを共用(二重実装の禁止 — ADR-0005)。
"""

import io
import json
import logging
import re

import oracledb
from fastapi import HTTPException  # jetuse_core.authが投げる例外の捕捉に使用
from fdk import response

from jetuse_core import audit, nl2sql, tts
from jetuse_core import presets as preset_repo
from jetuse_core.auth import verify_token
from jetuse_core.logging import configure
from jetuse_core.settings import get_settings

configure()
logger = logging.getLogger("jetuse.fn.router")


def _json(ctx, body: dict | list, status: int = 200):
    return response.Response(
        ctx, response_data=json.dumps(body, ensure_ascii=False),
        headers={"Content-Type": "application/json"}, status_code=status,
    )


def _error(ctx, status: int, detail: str):
    return _json(ctx, {"detail": detail}, status)


def handler(ctx, data: io.BytesIO):
    headers = {k.lower(): v for k, v in (ctx.Headers() or {}).items()}
    method = (headers.get("fn-http-method") or "GET").upper()
    url = headers.get("fn-http-request-url") or ""
    path = url.split("?")[0]
    auth_header = headers.get("fn-http-h-authorization") or headers.get("authorization") or ""
    token = auth_header.removeprefix("Bearer ").strip() or None

    try:
        user = verify_token(token, get_settings())
    except HTTPException as e:
        return _error(ctx, e.status_code, str(e.detail))

    try:
        body = json.loads(data.getvalue() or b"{}")
    except json.JSONDecodeError:
        return _error(ctx, 400, "invalid json body")

    try:
        return _route(ctx, method, path, body, user.subject)
    except HTTPException as e:
        return _error(ctx, e.status_code, str(e.detail))
    except nl2sql.SqlRejectedError as e:
        return _error(ctx, 400, str(e))
    except tts.TtsError as e:
        # PORT-02: CI(FastAPI)側と同じ縮退(503+ヒント)。捕捉しないと下のExceptionで
        # 生の"internal error"500に潰れヒントが失われる(ADR-0005: 二重実装の禁止)。
        logger.warning("fn tts synthesize degraded: %s", e)
        return _error(ctx, 503, str(e))
    except oracledb.Error as e:
        msg = str(e).splitlines()[0][:300]
        if "DPY-" in msg:
            # 診断のため先頭行を含める(接続文字列等の機密は含まれない)
            return _error(ctx, 503, f"データベースに接続できません: {msg}")
        return _error(ctx, 400, f"SQL実行エラー: {msg}")
    except Exception:
        logger.exception("fn router failed: %s %s", method, path)
        return _error(ctx, 500, "internal error")


def _route(ctx, method: str, path: str, body: dict, owner: str):
    # --- presets (CHAT-04) ---
    if path == "/api/presets" and method == "GET":
        return _json(ctx, {"presets": preset_repo.list_presets(owner)})
    if path == "/api/presets" and method == "POST":
        name = (body.get("name") or "").strip()
        content = body.get("content") or ""
        if not name or not content or len(name) > 200:
            return _error(ctx, 422, "name(≤200字)とcontentは必須です")
        return _json(ctx, preset_repo.create_preset(owner, name, content))
    m = re.fullmatch(r"/api/presets/([0-9a-f-]{36})", path)
    if m and method == "DELETE":
        if not preset_repo.delete_preset(owner, m.group(1)):
            return _error(ctx, 404, "preset not found")
        return _json(ctx, {"deleted": True})

    # --- dbchat (SQL-02/03) ---
    if path == "/api/dbchat/schema" and method == "GET":
        # PORT-02: CI(FastAPI)側の /api/dbchat/schema と同じ契約に揃える
        # (sample_available/sample_unavailable_reason。ADR-0005: 二重実装の禁止)。
        info = nl2sql.get_schema_info()
        sample = nl2sql.sh_sample_status()
        return _json(ctx, {
            **info,
            "sample_available": sample["available"],
            **({"sample_unavailable_reason": sample["reason"]}
               if not sample["available"] else {}),
        })
    # feedback 20260620 #3: Select AIで選択可能なモデル一覧(dbchatセグメントはFn経由のため要追加)
    if path == "/api/dbchat/select-ai-models" and method == "GET":
        return _json(ctx, {"models": nl2sql.SELECT_AI_MODELS,
                           "default": nl2sql.DEFAULT_SELECT_AI_MODEL})
    if path == "/api/dbchat/execute" and method == "POST":
        sql = body.get("sql") or ""
        if not sql.strip() or len(sql) > 20000:
            return _error(ctx, 422, "sqlは必須(≤20000字)です")
        result = nl2sql.execute_readonly(sql)
        logger.info("fn dbchat executed rows=%s user=%s", result["row_count"], owner)
        audit.log_event(owner, "dbchat", meta=f"rows={result['row_count']}")
        return _json(ctx, result)
    if path == "/api/dbchat/chart" and method == "POST":
        columns = body.get("columns") or []
        if not columns:
            return _error(ctx, 422, "columnsは必須です")
        return _json(ctx, nl2sql.suggest_chart(
            body.get("question") or "", columns, body.get("rows") or []
        ))

    # --- tts (VOICE-03) ---
    if path == "/api/tts" and method == "POST":
        text = (body.get("text") or "").strip()
        voice = body.get("voice") or tts.DEFAULT_VOICE
        if not text or len(text) > tts.MAX_TEXT_CHARS:
            return _error(ctx, 422, f"textは必須(≤{tts.MAX_TEXT_CHARS}字)です")
        if voice not in tts.VOICES:
            return _error(ctx, 422, f"unknown voice (allowed: {', '.join(tts.VOICES)})")
        audio = tts.synthesize(text, voice)
        audit.log_event(owner, "tts", input_tokens=len(text), meta=voice)
        return response.Response(
            ctx, response_data=audio,
            headers={"Content-Type": "audio/mpeg"}, status_code=200,
        )

    return _error(ctx, 404, f"no route: {method} {path}")
