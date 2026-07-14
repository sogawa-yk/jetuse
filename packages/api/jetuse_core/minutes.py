"""議事録生成(VOICE-01): バッチ文字起こし(話者分離) + LLM整形。

SPIKE-06の実機確定事項:
- WHISPER_MEDIUM + languageCode + diarization が大阪で動作
- 出力は分かち書きトークン(token/startTime/endTime/speakerIndex) → 結合・空白除去の後処理必須
"""

import json
import logging
import os
import uuid
from typing import Any

from .db import connect
from .settings import get_settings

logger = logging.getLogger("jetuse.minutes")

ALLOWED_EXTENSIONS = {".mp3", ".wav", ".m4a", ".ogg", ".webm"}
MAX_BYTES = 100 * 1024 * 1024
# 整形プロンプトに入れるトランスクリプト上限(超過分は打ち切り注記)
MAX_TRANSCRIPT_CHARS = 24_000

TEMPLATES: dict[str, str] = {
    "minutes": (
        "以下の会議トランスクリプトから議事録をMarkdownで作成してください。\n"
        "構成: # 議事録 / ## 出席者(話者1,話者2..の発言傾向から役割を推測してよい) / "
        "## 決定事項 / ## TODO(可能なら担当の話者を付記) / ## 議論サマリ(時系列)。\n"
        "トランスクリプトに無い事実を創作しないこと。"
    ),
    "faq": (
        "以下のトランスクリプトから、質問と回答のペアを抽出しFAQをMarkdownで作成してください。\n"
        "構成: # FAQ / 各項目は ## Q: ... と A: ...。明示的な質疑がない場合は"
        "内容から想定問答を作り、その旨を冒頭に注記すること。"
    ),
    "article": (
        "以下のトランスクリプトを基に、ニュース記事をMarkdownで作成してください。\n"
        "構成: # 見出し / リード文(2-3文) / 本文(小見出しつき)。"
        "事実はトランスクリプトの範囲内に限ること。"
    ),
}


def _clients():
    import oci

    if os.environ.get("AUTH_MODE") == "resource_principal":
        signer = oci.auth.signers.get_resource_principals_signer()
        cfg = {"region": get_settings().oci_region}
        return (
            oci.object_storage.ObjectStorageClient(cfg, signer=signer),
            oci.ai_speech.AIServiceSpeechClient(cfg, signer=signer),
        )
    from .genai import load_local_oci_config

    cfg = load_local_oci_config()
    return (
        oci.object_storage.ObjectStorageClient(cfg),
        oci.ai_speech.AIServiceSpeechClient(cfg),
    )


def _require_bucket() -> str:
    bucket = get_settings().speech_bucket
    if not bucket:
        raise RuntimeError("SPEECH_BUCKET is not configured")
    return bucket


# --- ジョブ作成 ---


def create_job(owner: str, filename: str, content: bytes, language: str) -> dict[str, Any]:
    import oci.ai_speech.models as sm

    bucket = _require_bucket()
    os_client, sp_client = _clients()
    ns = os_client.get_namespace().data
    mid = str(uuid.uuid4())
    ext = os.path.splitext(filename)[1].lower()
    audio_object = f"minutes/{owner}/{mid}/audio{ext}"
    out_prefix = f"minutes/{owner}/{mid}/out"

    os_client.put_object(ns, bucket, audio_object, content)

    details = sm.CreateTranscriptionJobDetails(
        compartment_id=get_settings().compartment_ocid,
        display_name=f"jetuse-minutes-{mid}",
        input_location=sm.ObjectListInlineInputLocation(
            location_type="OBJECT_LIST_INLINE_INPUT_LOCATION",
            object_locations=[
                sm.ObjectLocation(
                    namespace_name=ns, bucket_name=bucket, object_names=[audio_object]
                )
            ],
        ),
        output_location=sm.OutputLocation(
            namespace_name=ns, bucket_name=bucket, prefix=out_prefix
        ),
        model_details=sm.TranscriptionModelDetails(
            model_type="WHISPER_MEDIUM",
            language_code=language,
            transcription_settings=sm.TranscriptionSettings(
                diarization=sm.Diarization(is_diarization_enabled=True)
            ),
        ),
    )
    try:
        job = sp_client.create_transcription_job(details).data
    except Exception:
        # ジョブが作れない場合は入力オブジェクトを残さない
        try:
            os_client.delete_object(ns, bucket, audio_object)
        except Exception:
            logger.exception("orphan audio cleanup failed (ignored)")
        raise

    with connect() as conn:
        conn.cursor().execute(
            """
            INSERT INTO minutes_jobs(id, owner_sub, title, status, language,
                                     audio_object, oci_job_id)
            VALUES (:id, :o, :t, 'processing', :lang, :obj, :job)
            """,
            id=mid, o=owner, t=filename[:400], lang=language[:10],
            obj=audio_object, job=job.id,
        )
        conn.commit()
    return {"id": mid, "title": filename, "status": "processing", "language": language}


# --- 一覧・取得(状態同期) ---


# 一覧時に状態同期するprocessing行の上限(通常0〜1行。OCIへのGETが行数分発生するため)
MAX_LIST_SYNC = 5


def list_jobs(owner: str) -> list[dict[str, Any]]:
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, title, status, language, audio_object, oci_job_id,
                   TO_CHAR(created_at, 'YYYY-MM-DD HH24:MI')
            FROM minutes_jobs WHERE owner_sub = :o ORDER BY created_at DESC
            FETCH FIRST 50 ROWS ONLY
            """,
            o=owner,
        )
        rows = cur.fetchall()
    jobs = []
    synced = 0
    for r in rows:
        rec = {"id": r[0], "title": r[1], "status": r[2], "language": r[3], "created_at": r[6]}
        # 一覧でも処理中ジョブはOCI状態へ同期する(詳細を開かない限りバッジが
        # 「処理中」のまま残る問題の対策 — ユーザー報告 2026-06-12)
        if rec["status"] == "processing" and r[5] and synced < MAX_LIST_SYNC:
            synced += 1
            rec = _sync_status(owner, rec, audio_object=r[4], oci_job_id=r[5])
        jobs.append(
            {k: rec[k] for k in ("id", "title", "status", "language", "created_at")}
        )
    return jobs


def get_job(owner: str, mid: str) -> dict[str, Any] | None:
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, title, status, language, audio_object, oci_job_id,
                   speaker_count, transcript, error,
                   TO_CHAR(created_at, 'YYYY-MM-DD HH24:MI')
            FROM minutes_jobs WHERE id = :id AND owner_sub = :o
            """,
            id=mid, o=owner,
        )
        row = cur.fetchone()
    if not row:
        return None
    rec = {
        "id": row[0], "title": row[1], "status": row[2], "language": row[3],
        "speaker_count": row[6],
        "transcript": json.loads(row[7]) if row[7] else None,
        "error": row[8], "created_at": row[9],
    }
    if rec["status"] == "processing" and row[5]:
        rec = _sync_status(owner, rec, audio_object=row[4], oci_job_id=row[5])
    return rec


def _sync_status(
    owner: str, rec: dict[str, Any], audio_object: str, oci_job_id: str
) -> dict[str, Any]:
    """OCIジョブの状態をポーリング同期。SUCCEEDEDなら結果を取り込み確定させる。"""
    try:
        _, sp_client = _clients()
        job = sp_client.get_transcription_job(oci_job_id).data
        state = job.lifecycle_state
    except Exception:
        logger.exception("speech job polling failed")
        return rec  # 一時障害は状態を変えない

    if state in ("ACCEPTED", "IN_PROGRESS"):
        return rec
    if state == "SUCCEEDED":
        try:
            utterances, speaker_count = _fetch_result(audio_object, oci_job_id)
            _update(
                rec["id"], owner, status="completed",
                transcript=json.dumps(utterances, ensure_ascii=False),
                speaker_count=speaker_count,
            )
            rec.update(
                status="completed", transcript=utterances, speaker_count=speaker_count
            )
        except Exception as e:
            logger.exception("speech result fetch failed")
            _update(rec["id"], owner, status="failed", error=f"result fetch: {e}"[:1000])
            rec.update(status="failed", error=str(e)[:1000])
    else:  # FAILED / CANCELED 等
        msg = f"transcription {state}"
        _update(rec["id"], owner, status="failed", error=msg)
        rec.update(status="failed", error=msg)
    return rec


def _update(mid: str, owner: str, **cols: Any) -> None:
    sets = ", ".join(f"{k} = :{k}" for k in cols)
    with connect() as conn:
        conn.cursor().execute(
            f"UPDATE minutes_jobs SET {sets} WHERE id = :id AND owner_sub = :o",  # noqa: S608
            id=mid, o=owner, **cols,
        )
        conn.commit()


# --- 結果JSONの取得と後処理(分かち書き対策) ---


def _fetch_result(audio_object: str, oci_job_id: str) -> tuple[list[dict], int]:
    bucket = _require_bucket()
    os_client, _ = _clients()
    ns = os_client.get_namespace().data
    # 出力prefixは入力オブジェクトの親 + /out。実ファイル名はジョブ依存のため一覧から特定
    prefix = audio_object.rsplit("/", 1)[0] + "/out"
    objects = os_client.list_objects(ns, bucket, prefix=prefix, fields="name").data.objects
    json_names = [o.name for o in objects if o.name.endswith(".json")]
    if not json_names:
        raise RuntimeError(f"no output json under {prefix}")
    body = os_client.get_object(ns, bucket, json_names[0]).data.content
    data = json.loads(body)
    tr = (data.get("transcriptions") or [{}])[0]
    tokens = tr.get("tokens") or []
    speaker_count = int(tr.get("speakerCount") or 0)
    return _to_utterances(tokens), speaker_count


def _parse_time(v: Any) -> float:
    """'12.34s' / '1234ms' / 数値 を秒(float)へ"""
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if s.endswith("ms"):
        return float(s[:-2]) / 1000
    if s.endswith("s"):
        return float(s[:-1])
    return float(s)


def _to_utterances(tokens: list[dict]) -> list[dict]:
    """speakerIndexの連続区間でまとめ、{speaker,start,end,text}の配列にする。

    Whisper出力は分かち書き(全トークンWORD扱い・日本語もスペース区切り)のため、
    トークンを結合して空白を除去する。ASCII語同士の境界のみスペースを保つ。
    """
    utterances: list[dict] = []
    cur: dict | None = None
    for tk in tokens:
        text = (tk.get("token") or "").strip()
        if not text:
            continue
        sp = int(tk.get("speakerIndex") or 0)
        start = _parse_time(tk.get("startTime"))
        end = _parse_time(tk.get("endTime"))
        if cur is None or cur["speaker"] != sp:
            if cur:
                utterances.append(cur)
            cur = {"speaker": sp, "start": start, "end": end, "parts": [text]}
        else:
            cur["parts"].append(text)
            cur["end"] = end
    if cur:
        utterances.append(cur)
    for u in utterances:
        u["text"] = _join_tokens(u.pop("parts"))
    return utterances


def _is_ascii_word(s: str) -> bool:
    return bool(s) and all(c.isascii() and (c.isalnum() or c in "'-") for c in s)


def _join_tokens(parts: list[str]) -> str:
    out = ""
    prev_ascii = False
    for p in parts:
        cur_ascii = _is_ascii_word(p)
        if out and prev_ascii and cur_ascii:
            out += " "
        out += p
        prev_ascii = cur_ascii
    return out


# --- 削除 ---


def delete_job(owner: str, mid: str) -> bool:
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT audio_object FROM minutes_jobs WHERE id = :id AND owner_sub = :o",
            id=mid, o=owner,
        )
        row = cur.fetchone()
        if not row:
            return False
        cur.execute(
            "DELETE FROM minutes_jobs WHERE id = :id AND owner_sub = :o", id=mid, o=owner
        )
        conn.commit()
    # バケット側の音声+結果はベストエフォートで削除
    try:
        bucket = _require_bucket()
        os_client, _ = _clients()
        ns = os_client.get_namespace().data
        prefix = row[0].rsplit("/", 1)[0]  # minutes/{owner}/{id}
        for obj in os_client.list_objects(ns, bucket, prefix=prefix, fields="name").data.objects:
            os_client.delete_object(ns, bucket, obj.name)
    except Exception:
        logger.exception("minutes objects cleanup failed (ignored)")
    return True


# --- LLM整形(generate)用メッセージ構築 ---


def build_generation_messages(
    utterances: list[dict], template: str, title: str
) -> list[dict]:
    instruction = TEMPLATES[template]
    lines = []
    for u in utterances:
        m, s = divmod(int(u["start"]), 60)
        lines.append(f"[{m:02d}:{s:02d}] 話者{int(u['speaker']) + 1}: {u['text']}")
    transcript = "\n".join(lines)
    truncated = len(transcript) > MAX_TRANSCRIPT_CHARS
    if truncated:
        transcript = transcript[:MAX_TRANSCRIPT_CHARS]
        instruction += (
            "\n注意: トランスクリプトは長さ上限で途中までです。冒頭にその旨を注記すること。"
        )
    user_content = (
        f"{instruction}\n\n## 元ファイル: {title}\n\n## トランスクリプト\n{transcript}"
    )
    return [
        {"role": "system", "content": "あなたは会議内容を正確に整理する日本語アシスタントです。"},
        {"role": "user", "content": user_content},
    ]
