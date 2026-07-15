"""モデル可用性のlazyマーク(PORT-02)。

chat.py:stream_chat での NotFound/PermissionDenied 検知→models._unavailable への記録→
GET /api/chat/models と POST /api/chat/stream への反映(既定モデルはフォールバック、
それ以外はヒント付きエラー)を検証する。
"""

import types

import httpx
import openai
import pytest
from fastapi.testclient import TestClient

import service.main as service_main
import service.routes.chat as chat_routes
from jetuse_core import chat as chat_mod
from jetuse_core import models
from jetuse_core.models import DEFAULT_MODEL
from service.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_unavailable():
    models.clear_unavailable()
    yield
    models.clear_unavailable()


def _api_error(cls, status):
    req = httpx.Request("POST", "https://genai.test/v1/x")
    resp = httpx.Response(status, request=req, json={"message": "denied"})
    return cls("denied", response=resp, body=None)


def test_model_status_default_available():
    ok, hint = models.model_status(DEFAULT_MODEL)
    assert ok is True
    assert hint is None


def test_mark_unavailable_and_clear():
    models.mark_unavailable("gemini-2.5-pro", "HTTP 404")
    ok, hint = models.model_status("gemini-2.5-pro")
    assert ok is False
    assert hint == "HTTP 404"
    models.clear_unavailable("gemini-2.5-pro")
    assert models.model_status("gemini-2.5-pro")[0] is True


def test_stream_chat_marks_unavailable_on_not_found(monkeypatch):
    def _raise(**kw):
        raise _api_error(openai.NotFoundError, 404)

    fake_client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=_raise))
    )
    monkeypatch.setattr(chat_mod, "make_inference_client", lambda **kw: fake_client)
    events = list(
        chat_mod.stream_chat("llama-3.3-70b", [{"role": "user", "content": "hi"}])
    )
    assert any("利用できません" in ev.get("error", "") for ev in events)
    ok, hint = models.model_status("llama-3.3-70b")
    assert ok is False
    assert hint == "HTTP 404"


def test_stream_chat_rag_404_does_not_poison_model_availability(monkeypatch):
    """stale vector store等RAG起因の404はモデル不可と誤記録しない(レビュー指摘F-001)。"""
    from jetuse_core.chat import GenParams

    def _raise(**kw):
        raise _api_error(openai.NotFoundError, 404)

    fake_client = types.SimpleNamespace(
        responses=types.SimpleNamespace(create=_raise)
    )
    monkeypatch.setattr(chat_mod, "make_inference_client", lambda **kw: fake_client)
    events = list(
        chat_mod.stream_chat(
            "gpt-oss-120b", [{"role": "user", "content": "hi"}],
            params=GenParams(file_search_store="vs_stale"),
        )
    )
    assert any(ev.get("error") for ev in events)
    ok, hint = models.model_status("gpt-oss-120b")
    assert ok is True
    assert hint is None


def test_stream_chat_project_scoped_404_does_not_poison_model_availability(monkeypatch):
    """エージェント固有project_ocidの誤設定/権限不足による404もモデル不可と誤記録しない
    (レビュー指摘F-001)。"""

    def _raise(**kw):
        raise _api_error(openai.NotFoundError, 404)

    fake_client = types.SimpleNamespace(
        responses=types.SimpleNamespace(create=_raise)
    )
    monkeypatch.setattr(chat_mod, "make_inference_client", lambda **kw: fake_client)
    events = list(
        chat_mod.stream_chat(
            "gpt-oss-120b", [{"role": "user", "content": "hi"}],
            project_ocid="ocid1.generativeaiproject.oc1..agent",
        )
    )
    assert any(ev.get("error") for ev in events)
    ok, hint = models.model_status("gpt-oss-120b")
    assert ok is True
    assert hint is None


def test_stream_chat_leaves_other_errors_untouched(monkeypatch):
    """400等(モデル不在以外)は可用性を落とさず、そのままのエラーを伝える。"""

    def _raise(**kw):
        raise _api_error(openai.BadRequestError, 400)

    fake_client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=_raise))
    )
    monkeypatch.setattr(chat_mod, "make_inference_client", lambda **kw: fake_client)
    list(chat_mod.stream_chat("llama-3.3-70b", [{"role": "user", "content": "hi"}]))
    assert models.model_status("llama-3.3-70b")[0] is True


def test_mark_unavailable_self_heals_after_ttl_expires():
    """レビュー指摘(再発): マークしたままだとルート側が実呼び出し自体をスキップするため
    TTL切れで自動的に再試行対象へ戻る(プロセス再起動なしでの自己回復)ことを確認する。"""
    models.mark_unavailable("gemini-2.5-pro", "HTTP 404")
    assert models.model_status("gemini-2.5-pro")[0] is False
    # TTL経過をシミュレート(内部stateを直接操作。時計を書き換えない)
    hint, _retry_at = models._unavailable["gemini-2.5-pro"]
    models._unavailable["gemini-2.5-pro"] = (hint, models.time.monotonic() - 1)
    ok, hint2 = models.model_status("gemini-2.5-pro")
    assert ok is True
    assert hint2 is None
    # PORT-02レビュー指摘: 読み取り専用のmodel_status()は_unavailableを書き換えない
    # (期限切れの判定のみ。エントリは残っていてよい — 次のmark_unavailable()で上書きされる)
    assert "gemini-2.5-pro" in models._unavailable


def test_list_models_reports_available_flag():
    models.mark_unavailable("gemini-2.5-pro", "HTTP 404")
    res = client.get("/api/chat/models")
    body = {m["key"]: m for m in res.json()["models"]}
    assert body["gemini-2.5-pro"]["available"] is False
    assert body["gemini-2.5-pro"]["unavailable_reason"] == "HTTP 404"
    assert body[DEFAULT_MODEL]["available"] is True
    assert "unavailable_reason" not in body[DEFAULT_MODEL]


def test_chat_stream_select_ai_rag_bypasses_model_availability_check(monkeypatch):
    """PORT-02レビュー指摘(再発): select_ai/opensearch RAGはreq.modelを一切使わないため、
    既定モデルがunavailableでもブロックしてはいけない。"""
    models.mark_unavailable(DEFAULT_MODEL, "HTTP 404")
    # dev 統合(M007): select_ai RAG 分岐は owner_key_gate(DB) を通すためテストでは no-op 化
    monkeypatch.setattr(chat_routes, "owner_key_gate", lambda: None)

    def fake_generate(owner, prompt):
        return "回答本文です。", []

    monkeypatch.setattr(service_main.rag_select_ai, "generate", fake_generate)
    res = client.post(
        "/api/chat/stream",
        json={
            "model": DEFAULT_MODEL,
            "messages": [{"role": "user", "content": "q"}],
            "rag": True,
            "rag_backend": "select_ai",
        },
    )
    assert res.status_code == 200
    assert '"delta": "回答本文です。"' in res.text
    assert "利用できません" not in res.text


def test_chat_stream_non_default_unavailable_returns_hint_sse():
    models.mark_unavailable("gemini-2.5-pro", "HTTP 404")
    res = client.post(
        "/api/chat/stream",
        json={"model": "gemini-2.5-pro", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert res.status_code == 200
    assert "利用できません" in res.text
    assert "HTTP 404" in res.text


def test_chat_stream_default_unavailable_with_conversation_id_skips_fallback(monkeypatch):
    """PORT-02 レビュー指摘F-001: 会話メモリ付きリクエストはresponses-family必須のため、
    既定モデル不可時にchat-familyへ黙ってフォールバックしない(短期メモリのサイレント無効化防止)。
    dev 統合: 所有者・箱スコープ検証(owner_key_gate+会話照合)が全早期 return より前に走るため
    (review-1 M001 / review-2 B002)、ゲートを no-op 化し会話照合は fake を返す。"""
    models.mark_unavailable(DEFAULT_MODEL, "HTTP 404")
    monkeypatch.setattr(chat_routes, "owner_key_gate", lambda: None)
    monkeypatch.setattr(
        chat_routes.conv_repo, "get_conversation",
        lambda owner, cid, demo_id=None: {"id": cid, "model": DEFAULT_MODEL,
                                          "oci_conversation_id": None},
    )
    res = client.post(
        "/api/chat/stream",
        json={
            "model": DEFAULT_MODEL,
            "conversation_id": "conv-1",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert res.status_code == 200
    assert "利用できません" in res.text
    assert "フォールバック" not in res.text


def test_chat_stream_default_unavailable_falls_back_to_chat_family(monkeypatch):
    models.mark_unavailable(DEFAULT_MODEL, "HTTP 404")
    seen_model = {}

    def fake_stream(model_key, messages, temperature=None, user="",
                    oci_conversation_id=None, params=None):
        seen_model["key"] = model_key
        yield {"delta": "ok"}

    monkeypatch.setattr(service_main, "stream_chat", fake_stream)
    res = client.post(
        "/api/chat/stream",
        json={"model": DEFAULT_MODEL, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert res.status_code == 200
    assert "フォールバック" in res.text
    assert '"delta": "ok"' in res.text
    assert seen_model["key"] == "llama-3.3-70b"  # MODELS登録順で最初のchat系
