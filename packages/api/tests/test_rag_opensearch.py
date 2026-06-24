"""OpenSearch RAG(ENH-05)のチャンク・抽出・生成ロジックの単体テスト(外部呼び出しはモック)。"""

from types import SimpleNamespace as NS
from unittest import mock

from jetuse_core import rag_opensearch


def test_enabled_reflects_endpoint(monkeypatch):
    monkeypatch.setattr(rag_opensearch, "get_settings",
                        lambda: NS(opensearch_endpoint=""))
    assert rag_opensearch.enabled() is False
    monkeypatch.setattr(rag_opensearch, "get_settings",
                        lambda: NS(opensearch_endpoint="http://10.0.0.1:9200"))
    assert rag_opensearch.enabled() is True


def test_extract_text_txt():
    assert rag_opensearch._extract_text("a.txt", "こんにちは".encode()) == "こんにちは"


def test_chunk_overlap():
    text = "あ" * 2000
    chunks = rag_opensearch._chunk(text)
    assert len(chunks) >= 2
    assert all(len(c) <= rag_opensearch._CHUNK_CHARS for c in chunks)
    # 隣接チャンクはオーバーラップ分だけ重なる
    assert chunks[0][-rag_opensearch._CHUNK_OVERLAP:] == chunks[1][:rag_opensearch._CHUNK_OVERLAP]


def test_chunk_empty():
    assert rag_opensearch._chunk("   ") == []


def test_generate_no_hits_returns_message():
    with mock.patch.object(rag_opensearch, "search", return_value=[]):
        body, cites = rag_opensearch.generate("u", "質問")
    assert "見つかりません" in body
    assert cites == []


def test_generate_with_hits_builds_answer_and_dedup_citations():
    hits = [
        {"text": "規程A本文", "filename": "kitei.pdf", "file_id": "f1", "score": 0.9},
        {"text": "規程A補足", "filename": "kitei.pdf", "file_id": "f1", "score": 0.8},
        {"text": "別表", "filename": "annex.md", "file_id": "f2", "score": 0.7},
    ]
    fake_resp = NS(choices=[NS(message=NS(content="回答本文"))])
    client = mock.Mock()
    client.chat.completions.create.return_value = fake_resp
    with mock.patch.object(rag_opensearch, "search", return_value=hits), \
            mock.patch.object(rag_opensearch, "make_inference_client", return_value=client):
        body, cites = rag_opensearch.generate("u", "質問")
    assert body == "回答本文"
    # filename重複は1件に集約(kitei.pdf, annex.md)
    assert [c["filename"] for c in cites] == ["kitei.pdf", "annex.md"]
