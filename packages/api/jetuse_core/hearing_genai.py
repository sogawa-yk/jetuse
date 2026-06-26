"""ヒアリングの GenAI 補助(HBD-01 / §6 の境界)。

§6 の原則「何を選ぶかはルール＋SA 確認、埋める/書く/寄せるは GenAI」に従い、本モジュールは
GenAI を**補助に限定**する:
  ① ヒアリングメモの要点抽出 → 各質問のデフォルト提案(`source=genai_suggested` で保存)。
  ② Q1=other(その他業務)時の最近傍 SBA 提案。
GenAI 不在/失敗でも決定ルール(recommend.py)だけで推薦は成立する。したがって本モジュールの
公開関数は**例外を投げず、失敗時は空(提案なし)を返す**フォールバック設計とする。

採点・選定の権限は持たない(提案するだけ。確定は SA が回答保存で行う)。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from .hearing_schema import ANSWERABLE_IDS, QUESTIONS_BY_ID, validate_answer
from .models import MODELS
from .recommend import Q1_TO_SBA

logger = logging.getLogger("jetuse.hearing")

#: メモの上限(プロンプト肥大防止)。recommend 入力でなくプロンプト埋め込みのための切り詰め。
MAX_NOTES_PROMPT_CHARS = 6000


def _completer(model_key: str, system: str, user: str, max_chars: int) -> str:
    """非ストリーミング単発補完。import を遅延し、テストが差し替えやすいよう薄く包む。"""
    from .chat import complete_once

    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    return complete_once(model_key, messages, max_chars=max_chars) or ""


def _strip_json(raw: str) -> Any:
    """LLM 応答から JSON オブジェクトを取り出す(コードフェンス/前後文を許容)。失敗時 None。"""
    text = raw.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    # 最初の '{' から対応する最後の '}' までを試す。
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return None


def _questions_prompt() -> str:
    """質問と選択肢 id を列挙したプロンプト断片(LLM に id で答えさせる)。"""
    lines = []
    for qid in ANSWERABLE_IDS:
        q = QUESTIONS_BY_ID[qid]
        opts = ", ".join(f"{o.id}({o.label})" for o in q.options)
        kind = "複数選択(配列)" if q.type == "multi" else "単一選択"
        lines.append(f"- {qid} [{kind}]: {q.text} 選択肢: {opts}")
    return "\n".join(lines)


def suggest_answers_from_notes(
    notes: str, *, model_key: str
) -> dict[str, Any]:
    """ヒアリングメモから各質問のデフォルト回答を提案する(①)。{question_id: 正規化値}。

    LLM が返した選択肢 id を `validate_answer` で検証し、妥当なものだけを残す(未知 id は捨てる)。
    GenAI 不在/失敗/解析不能なら **空辞書**(提案なし=フォールバック)。例外は投げない。
    """
    notes = (notes or "").strip()
    if not notes:
        return {}
    system = (
        "あなたは営業支援のアシスタント。顧客ヒアリングのメモから、各質問の最も妥当な選択肢を"
        "選び、選択肢の id だけを使って JSON で答える。確信が持てない質問は出力に含めない。"
        "単一選択は文字列、複数選択は id の配列。余計な説明は出力しない。"
    )
    user = (
        f"# 質問と選択肢\n{_questions_prompt()}\n\n"
        f"# ヒアリングメモ\n{notes[:MAX_NOTES_PROMPT_CHARS]}\n\n"
        '# 出力形式(例)\n{"Q1": "support", "Q2": ["docs"], "Q3": "rag_qa"}'
    )
    try:
        raw = _completer(model_key, system, user, max_chars=1000)
    except Exception as e:  # noqa: BLE001 - GenAI 失敗は握ってフォールバック(推薦は成立)
        logger.warning("hearing genai suggest failed: %s", str(e).splitlines()[0][:200])
        return {}
    data = _strip_json(raw)
    if not isinstance(data, dict):
        return {}
    suggestions: dict[str, Any] = {}
    for qid, value in data.items():
        if qid not in ANSWERABLE_IDS:
            continue
        try:
            suggestions[qid] = validate_answer(qid, value)
        except Exception:  # noqa: BLE001 - 妥当でない提案は黙って捨てる(部分提案を許容)
            continue
    return suggestions


def nearest_sample_app(notes: str, *, model_key: str) -> str | None:
    """Q1=other 時、メモから最近傍の SBA を提案する(②)。SBA-A/B/C/D のいずれか、無ければ None。

    GenAI 不在/失敗や語彙外の応答なら None(決定ルールは sample_app=None のまま=フォールバック)。
    """
    notes = (notes or "").strip()
    if not notes:
        return None
    valid = {v for v in Q1_TO_SBA.values() if v}  # {SBA-A,SBA-B,SBA-C,SBA-D}
    system = (
        "顧客の業務メモから、最も近いサンプル業務アプリを1つ選ぶ。"
        "候補は SBA-A(顧客対応/サポート), SBA-B(在庫・受発注・データ照会), "
        "SBA-C(営業・案件), SBA-D(経理・帳票・経費)。"
        "コード(SBA-A など)だけを出力する。"
    )
    try:
        raw = _completer(model_key, system, notes[:MAX_NOTES_PROMPT_CHARS], max_chars=20)
    except Exception as e:  # noqa: BLE001
        logger.warning("hearing genai nearest failed: %s", str(e).splitlines()[0][:200])
        return None
    m = re.search(r"SBA-[ABCD]", raw.upper())
    if m and m.group(0) in valid:
        return m.group(0)
    return None


def _resolve_model(model_key: str | None) -> str:
    """既定モデル(sample_app_model)へ解決する。未知モデルは既定にフォールバック。"""
    from .settings import get_settings

    key = model_key or get_settings().sample_app_model
    return key if key in MODELS else get_settings().sample_app_model
