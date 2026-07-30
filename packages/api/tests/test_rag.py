"""RAG(RAG-01/02)のAPIテスト。rag層はfake、citations抽出は実関数。"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import service.main as service_main
from jetuse_core.chat import _extract_citations
from service.main import app

client = TestClient(app)


class FakeRag:
    def __init__(self):
        self.files: dict[str, dict] = {}
        self.store_id: str | None = None
        self.last_attributes: dict | None = None

    def list_files(self, owner):
        return [dict(v) for v in self.files.values()]

    def refresh_statuses(self, owner, files):
        return files

    def add_file(self, owner, filename, content, attributes=None):
        self.last_attributes = attributes
        fid = f"f{len(self.files) + 1}"
        self.files[fid] = {
            "id": fid, "filename": filename, "status": "processing",
            "bytes": len(content), "oci_file_id": f"file-{fid}",
        }
        self.store_id = self.store_id or "vs_fake"
        return self.files[fid]

    def delete_file(self, owner, file_id):
        return self.files.pop(file_id, None) is not None

    def get_store_id(self, owner):
        return self.store_id


@pytest.fixture(autouse=True)
def fake_rag(monkeypatch):
    fake = FakeRag()
    for name in ("list_files", "refresh_statuses", "add_file", "delete_file", "get_store_id"):
        monkeypatch.setattr(service_main.rag, name, getattr(fake, name))
    yield fake


def test_upload_list_delete(fake_rag):
    res = client.post(
        "/api/rag/files",
        files={"file": ("policy.md", b"# regulations", "text/markdown")},
    )
    assert res.status_code == 200
    fid = res.json()["id"]
    assert res.json()["status"] == "processing"
    assert any(f["id"] == fid for f in client.get("/api/rag/files").json()["files"])
    assert client.delete(f"/api/rag/files/{fid}").json() == {"deleted": True}
    assert client.delete(f"/api/rag/files/{fid}").status_code == 404


def test_upload_rejects_bad_files():
    res = client.post(
        "/api/rag/files", files={"file": ("doc.docx", b"x", "application/octet-stream")}
    )
    assert res.status_code == 422
    assert "docx" in res.json()["detail"]
    res2 = client.post("/api/rag/files", files={"file": ("a.exe", b"x", "x")})
    assert res2.status_code == 422
    res3 = client.post("/api/rag/files", files={"file": ("a.md", b"", "x")})
    assert res3.status_code == 422


def test_upload_returns_503_when_store_not_ready(monkeypatch):
    def not_ready(owner, filename, content, attributes=None):
        raise service_main.rag.StoreNotReadyError("dp propagation timeout")

    monkeypatch.setattr(service_main.rag, "add_file", not_ready)
    res = client.post("/api/rag/files", files={"file": ("a.md", b"x", "text/markdown")})
    assert res.status_code == 503
    assert "not ready" in res.json()["detail"]


def test_rag_chat_requires_responses_model_and_store(fake_rag, monkeypatch):
    body = {"model": "llama-3.3-70b", "messages": [{"role": "user", "content": "q"}], "rag": True}
    assert client.post("/api/chat/stream", json=body).status_code == 400  # chat系は不可

    body2 = {"model": "gpt-oss-120b", "messages": [{"role": "user", "content": "q"}], "rag": True}
    assert client.post("/api/chat/stream", json=body2).status_code == 400  # ストア未作成

    fake_rag.store_id = "vs_fake"
    captured = {}

    def fake_stream(model_key, messages, temperature=None, user="",
                    oci_conversation_id=None, params=None):
        captured["store"] = params.file_search_store
        yield {"delta": "ok"}

    monkeypatch.setattr(service_main, "stream_chat", fake_stream)
    assert client.post("/api/chat/stream", json=body2).status_code == 200
    assert captured["store"] == "vs_fake"


def test_select_ai_backend_streams_single_delta(monkeypatch):
    def fake_generate(owner, prompt):
        return "回答本文です。", [{"file_id": "a.md", "filename": "a.md", "score": None}]

    monkeypatch.setattr(service_main.rag_select_ai, "generate", fake_generate)
    res = client.post(
        "/api/chat/stream",
        json={
            "model": "gpt-oss-120b",
            "messages": [{"role": "user", "content": "q"}],
            "rag": True,
            "rag_backend": "select_ai",
        },
    )
    assert res.status_code == 200
    assert '"delta": "回答本文です。"' in res.text
    assert '"citations"' in res.text
    assert res.text.rstrip().endswith("data: [DONE]")


def test_split_sources():
    from jetuse_core.rag_select_ai import split_sources

    ans = (
        "宿泊費の上限は12,000円です。\n\nSources:\n"
        "  - travel-policy.pdf (https://objectstorage.example/x)\n"
        "  - b36a17e0-a6f0-45fc-91e8-94dc88a15cbb_expense.md (https://objectstorage.example/y)\n"
    )
    body, cites = split_sources(ans)
    assert body == "宿泊費の上限は12,000円です。"
    # uuidプレフィックス({uuid}_name)は表示名から除去される
    assert [c["filename"] for c in cites] == ["travel-policy.pdf", "expense.md"]
    body2, cites2 = split_sources("Sourcesなしの回答")
    assert body2 == "Sourcesなしの回答" and cites2 == []


def test_extract_citations():
    response = SimpleNamespace(
        output=[
            SimpleNamespace(
                type="file_search_call",
                results=[
                    SimpleNamespace(file_id="f1", filename="policy.pdf", score=0.83),
                    SimpleNamespace(file_id="f1", filename="policy.pdf", score=0.51),
                    SimpleNamespace(file_id="f2", filename="rules.md", score=0.42),
                ],
            ),
            SimpleNamespace(
                type="message",
                content=[
                    SimpleNamespace(
                        annotations=[SimpleNamespace(file_id="f3", filename="extra.txt")]
                    )
                ],
            ),
        ]
    )
    cites = _extract_citations(response)
    assert [c["file_id"] for c in cites] == ["f1", "f2", "f3"]
    assert cites[0]["score"] == 0.83  # 同一ファイルは最大スコア


def test_attach_backend_status(monkeypatch):
    """3バックエンドの取り込み状況が各ファイルに付与される(ENH-05)。"""
    from jetuse_core import rag

    files = [
        {"id": "f1", "filename": "a.pdf", "status": "completed"},
        {"id": "f2", "filename": "b.pdf", "status": "processing"},
        {"id": "f3", "filename": "c.pdf", "status": "failed"},
    ]
    import jetuse_core.rag_opensearch as ros
    import jetuse_core.rag_select_ai as rsa
    monkeypatch.setattr(rsa, "indexed_file_ids", lambda owner: {"f1"})
    monkeypatch.setattr(ros, "enabled", lambda: True)
    monkeypatch.setattr(ros, "indexed_file_ids", lambda owner: {"f1", "f2"})

    out = rag.attach_backend_status("u", files)
    assert out[0]["backends"] == {"vector_store": "indexed", "select_ai": "indexed",
                                  "opensearch": "indexed"}
    assert out[1]["backends"] == {"vector_store": "pending", "select_ai": "pending",
                                  "opensearch": "indexed"}
    assert out[2]["backends"] == {"vector_store": "error", "select_ai": "pending",
                                  "opensearch": "pending"}


def test_resolve_citation_filenames(monkeypatch):
    """OCIが返す文字化けファイル名を、DBの元ファイル名へ解決する(石井FB #4)。"""
    from jetuse_core import rag

    monkeypatch.setattr(rag, "list_files", lambda owner: [
        {"id": "u1", "filename": "日本語の規程.pdf", "oci_file_id": "ocifile-1"},
        {"id": "u2", "filename": "手順書.md", "oci_file_id": "ocifile-2"},
    ])
    cites = [
        {"file_id": "ocifile-1", "filename": "garbled-mojibake", "score": 0.9},
        {"file_id": "u2", "filename": "garbled", "score": None},
        {"file_id": "unknown", "filename": "keep", "score": None},
    ]
    out = rag.resolve_citation_filenames("o", cites)
    assert out[0]["filename"] == "日本語の規程.pdf"
    assert out[1]["filename"] == "手順書.md"
    assert out[2]["filename"] == "keep"


def test_attach_backend_status_opensearch_disabled(monkeypatch):
    import jetuse_core.rag_opensearch as ros
    import jetuse_core.rag_select_ai as rsa
    from jetuse_core import rag
    monkeypatch.setattr(rsa, "indexed_file_ids", lambda owner: set())
    monkeypatch.setattr(ros, "enabled", lambda: False)
    out = rag.attach_backend_status("u", [{"id": "f1", "filename": "a", "status": "completed"}])
    assert out[0]["backends"]["opensearch"] == "disabled"


# --- RAGM-01: 構造化引用 / 版フィルタ / 属性付き取り込み ---


def _hit(**kw):
    """file_search_call.results の 1 件(SPIKE-M1 ①-c の実レスポンス形)。"""
    base = {
        "file_id": "f1", "filename": "c01__v2.0__current__spec.txt", "score": 0.85,
        "attributes": {
            "file": "在庫連携API仕様書.xlsx", "version": "2.0", "sheet": "API一覧",
            "cells": "B12:F12", "kind": "spec", "current_version": "Y",
        },
        "text": "在庫照会API GET /v1/inventory は最大200件まで返却する。",
        "additional_properties": {"vector_store_id": "vs_x", "chunk_id": "0_ad585b8f"},
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_extract_citations_returns_structured_source():
    """完了条件: file/version/sheet/cells が構造化された値として載る(本文埋め込みでない)。"""
    response = SimpleNamespace(
        output=[SimpleNamespace(type="file_search_call", results=[_hit()])]
    )
    (c,) = _extract_citations(response)
    assert c["source"] == {
        "file": "在庫連携API仕様書.xlsx", "version": "2.0", "sheet": "API一覧",
        "cells": "B12:F12", "kind": "spec", "current_version": "Y",
    }
    assert c["text"].startswith("在庫照会API")
    assert c["chunk_id"] == "0_ad585b8f"


def test_extract_citations_backward_compatible_shape():
    """既存フロントは {file_id, filename, score} を読む。拡張は追加のみで壊さない。"""
    response = SimpleNamespace(
        output=[
            SimpleNamespace(type="file_search_call", results=[
                _hit(),
                # 属性なしの旧データ(拡張フィールドは付かない)
                _hit(file_id="f2", filename="old.txt", score=0.4,
                     attributes=None, text=None, additional_properties=None),
            ]),
            SimpleNamespace(type="message", content=[
                SimpleNamespace(annotations=[
                    SimpleNamespace(file_id="f3", filename="extra.txt")
                ])
            ]),
        ]
    )
    cites = _extract_citations(response)
    assert [c["file_id"] for c in cites] == ["f1", "f2", "f3"]
    for c in cites:
        assert set(c) >= {"file_id", "filename", "score"}
    assert "source" not in cites[1] and "text" not in cites[1]
    assert cites[0]["score"] == 0.85


def test_extract_citations_keeps_top_scoring_chunk_of_a_file():
    """属性はファイル単位(①-a)。同一ファイルの複数ヒットは最上位スコアのものを採る。"""
    response = SimpleNamespace(output=[SimpleNamespace(type="file_search_call", results=[
        _hit(score=0.3, text="低スコアのチャンク",
             additional_properties={"chunk_id": "9_low"}),
        _hit(score=0.9, text="高スコアのチャンク",
             additional_properties={"chunk_id": "1_high"}),
    ])])
    (c,) = _extract_citations(response)
    assert c["score"] == 0.9 and c["chunk_id"] == "1_high"


def test_citation_text_is_truncated():
    from jetuse_core.chat import CITATION_TEXT_CHARS

    response = SimpleNamespace(output=[SimpleNamespace(
        type="file_search_call", results=[_hit(text="あ" * (CITATION_TEXT_CHARS + 50))]
    )])
    (c,) = _extract_citations(response)
    assert len(c["text"]) == CITATION_TEXT_CHARS


def test_file_search_tool_carries_filters():
    from jetuse_core.chat import GenParams, _extra_responses_params
    from jetuse_core.models import MODELS

    f = {"type": "eq", "key": "current_version", "value": "Y"}
    extra = _extra_responses_params(
        MODELS["gpt-oss-120b"],
        GenParams(file_search_store="vs_x", file_search_filters=f),
    )
    assert extra["tools"] == [
        {"type": "file_search", "vector_store_ids": ["vs_x"], "filters": f}
    ]
    # 未指定なら filters キー自体を送らない(既存挙動の維持)
    extra2 = _extra_responses_params(
        MODELS["gpt-oss-120b"], GenParams(file_search_store="vs_x")
    )
    assert extra2["tools"] == [{"type": "file_search", "vector_store_ids": ["vs_x"]}]


def test_chat_stream_rejects_filters_in_agent_mode(fake_rag):
    """エージェント経路は別ディスパッチでフィルタを渡す口が無い。
    素通しすると黙って無視されるので 400 で断る(レビュー F-001)。"""
    fake_rag.store_id = "vs_fake"
    f = {"type": "eq", "key": "current_version", "value": "Y"}
    res = client.post("/api/chat/stream", json={
        "model": "gpt-oss-120b", "messages": [{"role": "user", "content": "q"}],
        "rag": True, "agent": True, "rag_filters": f,
    })
    assert res.status_code == 400 and "agent mode" in res.json()["detail"]
    res2 = client.post("/api/chat/stream", json={
        "model": "gpt-oss-120b", "messages": [{"role": "user", "content": "q"}],
        "rag": True, "agent_id": "a1", "rag_filters": f,
    })
    assert res2.status_code == 400 and "agent mode" in res2.json()["detail"]


def test_upload_rejects_overlong_filename_with_422(monkeypatch):
    """自動補完する file(=ファイル名)が 512 文字超でも 500 にせず 422(レビュー F-002)。"""
    from jetuse_core import rag_metadata

    calls = {"n": 0}

    def add_file(owner, filename, content, attributes=None):
        calls["n"] += 1
        # 実物と同じ順序: 属性の組み立て(検証)が OCI 呼び出しより前に走る
        rag_metadata.normalize_attributes({"file": filename, **(attributes or {})})
        raise AssertionError("検証を通ってはいけない")

    monkeypatch.setattr(service_main.rag, "add_file", add_file)
    res = client.post(
        "/api/rag/files", files={"file": ("a" * 520 + ".md", b"x", "text/markdown")}
    )
    assert res.status_code == 422 and "512" in res.json()["detail"]
    assert calls["n"] == 1


def test_chat_stream_passes_rag_filters(fake_rag, monkeypatch):
    fake_rag.store_id = "vs_fake"
    captured = {}

    def fake_stream(model_key, messages, temperature=None, user="",
                    oci_conversation_id=None, params=None):
        captured["filters"] = params.file_search_filters
        yield {"delta": "ok"}

    monkeypatch.setattr(service_main, "stream_chat", fake_stream)
    res = client.post("/api/chat/stream", json={
        "model": "gpt-oss-120b", "messages": [{"role": "user", "content": "q"}],
        "rag": True,
        "rag_filters": {"type": "eq", "key": "current_version", "value": "Y"},
    })
    assert res.status_code == 200
    assert captured["filters"] == {"type": "eq", "key": "current_version", "value": "Y"}


def test_chat_stream_rejects_unknown_filter_key(fake_rag):
    """SPIKE-M1 ①-b: 未知キーは上流では 0 件になるだけ。アプリが 422 で弾く。"""
    fake_rag.store_id = "vs_fake"
    res = client.post("/api/chat/stream", json={
        "model": "gpt-oss-120b", "messages": [{"role": "user", "content": "q"}],
        "rag": True,
        "rag_filters": {"type": "eq", "key": "current_verison", "value": "Y"},
    })
    assert res.status_code == 422
    assert "current_verison" in res.text


def test_chat_stream_rejects_filters_without_vector_store_backend(fake_rag):
    """効かない組み合わせを黙って無視しない(旧版が混ざる事故を防ぐ)。"""
    fake_rag.store_id = "vs_fake"
    f = {"type": "eq", "key": "current_version", "value": "Y"}
    res = client.post("/api/chat/stream", json={
        "model": "gpt-oss-120b", "messages": [{"role": "user", "content": "q"}],
        "rag": True, "rag_backend": "select_ai", "rag_filters": f,
    })
    assert res.status_code == 400 and "select_ai" in res.json()["detail"]
    res2 = client.post("/api/chat/stream", json={
        "model": "gpt-oss-120b", "messages": [{"role": "user", "content": "q"}],
        "rag_filters": f,
    })
    assert res2.status_code == 400 and "rag=true" in res2.json()["detail"]


def test_upload_accepts_attributes_form_field(fake_rag):
    res = client.post(
        "/api/rag/files",
        files={"file": ("spec.md", b"# spec", "text/markdown")},
        data={"attributes": '{"version": "2.0", "cells": "B12:F12", "sheet": ""}'},
    )
    assert res.status_code == 200
    # 空値はキーごと落ちる。file/sha256 は rag.add_file 側で補う
    assert fake_rag.last_attributes == {"version": "2.0", "cells": "B12:F12"}


def test_upload_rejects_bad_attributes():
    for bad in ('{"versoin": "2.0"}', "not json", '["file"]', '{"cells": "' + "x" * 513 + '"}'):
        res = client.post(
            "/api/rag/files",
            files={"file": ("spec.md", b"# spec", "text/markdown")},
            data={"attributes": bad},
        )
        assert res.status_code == 422, bad


def test_upload_without_attributes_stays_backward_compatible(fake_rag):
    res = client.post("/api/rag/files", files={"file": ("a.md", b"x", "text/markdown")})
    assert res.status_code == 200
    assert fake_rag.last_attributes == {}
