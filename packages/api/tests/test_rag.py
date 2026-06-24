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

    def list_files(self, owner):
        return [dict(v) for v in self.files.values()]

    def refresh_statuses(self, owner, files):
        return files

    def add_file(self, owner, filename, content):
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
