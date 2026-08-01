"""RAG(RAG-01/02)のAPIテスト。rag層はfake、citations抽出は実関数。"""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import service.main as service_main
from jetuse_core.chat import _extract_citations

# 差し替え前の実体（autouse の fake_rag が module 属性を上書きするので import 時に掴む）
from jetuse_core.rag import add_file as real_add_file
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

    def add_file(self, owner, filename, content, attributes=None, ocr_engine=None):
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
    def not_ready(owner, filename, content, attributes=None, ocr_engine=None):
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
    """各バックエンドの取り込み状況が各ファイルに付与される(ENH-05 / RAGM-02でadb追加)。"""
    from jetuse_core import rag

    files = [
        {"id": "f1", "filename": "a.pdf", "status": "completed"},
        {"id": "f2", "filename": "b.pdf", "status": "processing"},
        {"id": "f3", "filename": "c.pdf", "status": "failed"},
    ]
    import jetuse_core.rag_adb as radb
    import jetuse_core.rag_opensearch as ros
    import jetuse_core.rag_select_ai as rsa
    monkeypatch.setattr(rsa, "indexed_file_ids", lambda owner: {"f1"})
    monkeypatch.setattr(ros, "enabled", lambda: True)
    monkeypatch.setattr(ros, "indexed_file_ids", lambda owner: {"f1", "f2"})
    monkeypatch.setattr(radb, "enabled", lambda: True)
    monkeypatch.setattr(radb, "indexed_file_ids", lambda owner: {"f1"})
    monkeypatch.setattr(radb, "errored_file_ids", lambda owner: set())  # DB へ触らせない

    out = rag.attach_backend_status("u", files)
    assert out[0]["backends"] == {"vector_store": "indexed", "select_ai": "indexed",
                                  "opensearch": "indexed", "adb": "indexed"}
    assert out[1]["backends"] == {"vector_store": "pending", "select_ai": "pending",
                                  "opensearch": "indexed", "adb": "pending"}
    assert out[2]["backends"] == {"vector_store": "error", "select_ai": "pending",
                                  "opensearch": "pending", "adb": "pending"}


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
    import jetuse_core.rag_adb as radb
    import jetuse_core.rag_opensearch as ros
    import jetuse_core.rag_select_ai as rsa
    from jetuse_core import rag
    monkeypatch.setattr(rsa, "indexed_file_ids", lambda owner: set())
    monkeypatch.setattr(ros, "enabled", lambda: False)
    monkeypatch.setattr(radb, "enabled", lambda: False)  # DB へ触らせない
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

    def add_file(owner, filename, content, attributes=None, ocr_engine=None):
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


# --- xlsx の取り込み口と抽出口(PREP-01) ---------------------------------------


def _workbook() -> bytes:
    """架空の仕様書ブック(複数シート)。顧客データは使わない。"""
    from tests.test_extract_xlsx import SPEC, build

    return build(SPEC)


def test_upload_accepts_xlsx(fake_rag):
    res = client.post(
        "/api/rag/files",
        files={"file": ("サンプル仕様書.xlsx", _workbook(),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert res.status_code == 200
    assert res.json()["filename"] == "サンプル仕様書.xlsx"


def _use_real_add_file(monkeypatch):
    """xlsx の検証は `rag.add_file` の先頭(OCI を呼ぶ前)で走るので、本物を使って確かめる。"""
    monkeypatch.setattr(service_main.rag, "add_file", real_add_file)


def test_upload_rejects_broken_xlsx_with_422(monkeypatch):
    _use_real_add_file(monkeypatch)
    res = client.post("/api/rag/files", files={"file": ("broken.xlsx", b"not a zip", "x")})
    assert res.status_code == 422
    assert "xlsx" in res.json()["detail"]


def test_unsupported_type_message_lists_xlsx():
    res = client.post("/api/rag/files", files={"file": ("a.pptx", b"x", "x")})
    detail = res.json()["detail"]
    # 受け口(rag.ALLOWED_EXTENSIONS)の全形式が出る。PREP-03 で画像を足したので列挙も伸びる
    assert res.status_code == 422
    for ext in ("pdf", "txt", "md", "xlsx", "png", "jpg", "jpeg"):
        assert ext in detail, ext


def test_extract_returns_chunks_without_ingesting(fake_rag):
    res = client.post(
        "/api/extract", files={"file": ("サンプル仕様書.xlsx", _workbook(), "x")}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["filename"] == "サンプル仕様書.xlsx"
    assert body["chunk_count"] == len(body["chunks"]) == 2
    assert {c["cells"] for c in body["chunks"]} == {"B12:D13", "C5:E5"}
    assert [c["sheet"] for c in body["chunks"]] == ["API一覧", "制約"]
    # 取り込みは起きない(台帳にファイルが増えない)
    assert fake_rag.files == {}


def test_extract_works_for_text_files_too():
    res = client.post("/api/extract", files={"file": ("note.md", "# 見出し\n本文".encode(), "x")})
    assert res.status_code == 200
    assert res.json()["chunks"][0]["sheet"] == "本文"


def test_extract_rejects_limit_excess_with_422_naming_the_limit(monkeypatch):
    """上限超過は**切り詰めず**に 422。どの上限かが detail から分かる。"""
    from jetuse_core import extract_xlsx

    monkeypatch.setattr(extract_xlsx, "MAX_CHUNKS", 1)
    res = client.post("/api/extract", files={"file": ("spec.xlsx", _workbook(), "x")})
    assert res.status_code == 422
    assert "limit=chunks" in res.json()["detail"]


def test_extract_requires_supported_extension():
    assert client.post(
        "/api/extract", files={"file": ("a.docx", b"x", "x")}
    ).status_code == 422


def test_upload_rejects_xlsx_over_chunk_char_limit(monkeypatch):
    from jetuse_core import extract_xlsx
    from tests.test_extract_xlsx import build

    monkeypatch.setattr(extract_xlsx, "MAX_CHUNK_CHARS", 50)
    _use_real_add_file(monkeypatch)
    res = client.post(
        "/api/rag/files",
        files={"file": ("wide.xlsx", build({"制約": [("A1", "あ" * 100)]}), "x")},
    )
    assert res.status_code == 422 and "limit=chunk_chars" in res.json()["detail"]


# --- マネージド側へ渡す形(ファイル単位の属性)。実 OCI は E2E で確認する -------


def test_prepare_upload_converts_xlsx_to_text_with_file_level_attributes():
    """マネージド Vector Store は Office 形式を受け付けない(SPIKE-03)ので抽出テキストを渡す。

    そのとき属性は**ファイル単位**にしかできない(SPIKE-M1 ①-a)。チャンクごとの
    セル範囲を載せて「セル単位で返る」ように見せない。
    """
    from jetuse_core import rag as rag_module

    name, body, attrs = rag_module.prepare_upload("サンプル仕様書.xlsx", _workbook())
    assert name == "サンプル仕様書.xlsx.txt"
    assert "600 req/min" in body.decode()
    assert attrs == {"sheet": "(ブック全体: 2 シート)", "cells": "(ブック全体)"}


def test_prepare_upload_passes_through_non_xlsx():
    from jetuse_core import rag as rag_module

    assert rag_module.prepare_upload("a.md", b"# x") == ("a.md", b"# x", {})


def test_prepare_upload_rejects_empty_workbook():
    from jetuse_core import extract_xlsx
    from jetuse_core import rag as rag_module
    from tests.test_extract_xlsx import build

    with pytest.raises(extract_xlsx.EmptyWorkbook):
        rag_module.prepare_upload("empty.xlsx", build({"空": []}))


def test_upload_rejects_user_supplied_sheet_or_cells_for_xlsx(monkeypatch):
    """導出したファイル単位の属性を利用者指定で上書きさせない(能力差の偽装を防ぐ)。"""
    _use_real_add_file(monkeypatch)
    res = client.post(
        "/api/rag/files",
        files={"file": ("spec.xlsx", _workbook(), "x")},
        data={"attributes": '{"sheet": "制約", "cells": "C5:E5", "version": "2.0"}'},
    )
    assert res.status_code == 422
    detail = res.json()["detail"]
    assert "cells" in detail and "sheet" in detail and "adb" in detail


def test_upload_keeps_other_attributes_for_xlsx(monkeypatch):
    """`sheet` / `cells` 以外(版・分類)は従来どおり利用者指定が通る。"""
    from jetuse_core import rag as rag_module

    sent: dict = {}
    monkeypatch.setattr(rag_module, "ensure_store", lambda owner: "vs_fake")
    monkeypatch.setattr(rag_module, "_backup_original", lambda *a: None)
    monkeypatch.setattr(rag_module, "_insert_file", lambda *a: None)
    monkeypatch.setattr(rag_module, "make_inference_client", lambda **kw: _FakeDp(sent))
    _use_real_add_file(monkeypatch)
    res = client.post(
        "/api/rag/files",
        files={"file": ("spec.xlsx", _workbook(), "x")},
        data={"attributes": '{"version": "2.0", "kind": "spec"}'},
    )
    assert res.status_code == 200
    assert sent["upload_name"] == "spec.xlsx.txt"
    assert sent["attributes"]["version"] == "2.0" and sent["attributes"]["kind"] == "spec"
    # 出典は**ファイル単位**(SPIKE-M1 ①-a)。チャンクごとのセル範囲は載らない
    assert sent["attributes"]["sheet"] == "(ブック全体: 2 シート)"
    assert sent["attributes"]["cells"] == "(ブック全体)"
    assert sent["attributes"]["file"] == "spec.xlsx"          # 台帳の表示名は元の xlsx


class _FakeDp:
    """Files API / Vector Store の最小スタブ(実 OCI 呼び出しは E2E で確認する)。"""

    def __init__(self, sent: dict):
        self.sent = sent
        outer = self

        class Files:
            def create(self, *, file, purpose):
                outer.sent["upload_name"] = file[0]
                outer.sent["upload_bytes"] = file[1]
                return SimpleNamespace(id="file-fake")

        class VsFiles:
            def create(self, *, vector_store_id, file_id, attributes):
                outer.sent["attributes"] = attributes

        self.files = Files()
        self.vector_stores = SimpleNamespace(files=VsFiles())


def test_kind_is_passed_to_the_adb_backend(monkeypatch):
    """同じ `kind` を両バックエンドへ入れる（分類の絞り込みがバックエンドで食い違わない）。"""
    from jetuse_core import rag as rag_module
    from jetuse_core import rag_adb

    sent: dict = {}
    seen: dict = {}
    monkeypatch.setattr(rag_module, "ensure_store", lambda owner: "vs_fake")
    monkeypatch.setattr(rag_module, "_backup_original", lambda *a: None)
    monkeypatch.setattr(rag_module, "_insert_file", lambda *a: None)
    monkeypatch.setattr(rag_module, "make_inference_client", lambda **kw: _FakeDp(sent))
    monkeypatch.setattr(rag_adb, "availability", lambda: rag_adb.READY)
    monkeypatch.setattr(rag_adb, "ingest",
                        lambda owner, fid, name, body, *, kind="doc", **kw:
                        seen.update(kind=kind))
    _use_real_add_file(monkeypatch)
    res = client.post(
        "/api/rag/files",
        files={"file": ("spec.xlsx", _workbook(), "x")},
        data={"attributes": '{"kind": "spec"}'},
    )
    assert res.status_code == 200
    assert seen["kind"] == "spec" == sent["attributes"]["kind"]


def test_kind_longer_than_the_adb_column_is_rejected():
    """`kind` は ADB 側が VARCHAR2(32)。**バイト長**で見る（BYTE セマンティクス想定）。"""
    for bad in ("k" * 33, "分類" * 6):        # 33 バイト / 36 バイト
        res = client.post(
            "/api/rag/files",
            files={"file": ("a.md", b"x", "text/markdown")},
            data={"attributes": json.dumps({"kind": bad})},
        )
        assert res.status_code == 422 and "32" in res.json()["detail"], bad


def test_kind_rejects_non_string_scalars(fake_rag):
    """RAGM-04: `kind` は文字列のみ。数値・真偽は 422（黙って文字列化しない）。

    取り込みは 1 リクエストで両バックエンドへ入るので、ここで断ることが
    「同じ入力なら両バックエンドとも同じ応答」になる唯一の門。
    """
    for value in (0, False, 1.5):
        res = client.post(
            "/api/rag/files",
            files={"file": ("a.md", b"x", "text/markdown")},
            data={"attributes": json.dumps({"kind": value})},
        )
        assert res.status_code == 422, value
        assert "string" in res.json()["detail"]
        assert fake_rag.last_attributes is None  # OCI も ADB も呼ばない


@pytest.mark.parametrize("backend", ["vector_store", "adb", "select_ai", "opensearch"])
def test_chat_rejects_non_string_kind_filter_on_every_backend(fake_rag, backend):
    """RAGM-04: 同じ不正な `kind` フィルタは、選んだバックエンドに関わらず 422。

    バックエンド別の 400（絞り込み非対応）より**先に**型を弾く。ここが分かれると
    「同じ条件が選んだ先で違う応答になる」が検索側に残る。
    """
    fake_rag.store_id = "vs_fake"
    res = client.post("/api/chat/stream", json={
        "model": "gpt-oss-120b", "messages": [{"role": "user", "content": "q"}],
        "rag": True, "rag_backend": backend,
        "rag_filters": {"type": "eq", "key": "kind", "value": 1},
    })
    assert res.status_code == 422, backend
    assert "string" in res.text


def test_long_filename_keeps_its_extension():
    """台帳へ収める切り詰めで拡張子を落とさない（形式で分岐する判定が誤らないため）。"""
    from jetuse_core import rag as rag_module

    name = "あ" * 200 + ".xlsx"                # 600 バイト超
    fitted = rag_module._fit(name)
    assert fitted.endswith(".xlsx") and len(fitted.encode()) <= 400
    assert rag_module.extract_xlsx.is_xlsx(fitted)


def test_extract_rejects_broken_pdf_with_422():
    res = client.post("/api/extract", files={"file": ("broken.pdf", b"%PDF-1.7 garbage", "x")})
    assert res.status_code == 422 and "PDF" in res.json()["detail"]


def test_select_ai_badge_treats_xlsx_like_any_other_format(monkeypatch):
    """xlsx を拡張子だけで `error` にしない（PREP-02 の実測で恒久 error を撤回した）。

    実測（`docs/verification/PREP-02.md`）: Select AI の索引は xlsx の原本を
    Oracle Text 経由でテキスト化して取り込み、検索でも引ける。したがって xlsx の状態は
    他形式と同じく「索引に在るか」だけで決まる（未反映なら同期待ちの `pending`）。
    """
    import jetuse_core.rag_adb as radb
    import jetuse_core.rag_opensearch as ros
    import jetuse_core.rag_select_ai as rsa
    from jetuse_core import rag as rag_module

    monkeypatch.setattr(rsa, "indexed_file_ids", lambda owner: {"f1"})
    monkeypatch.setattr(ros, "enabled", lambda: False)
    monkeypatch.setattr(radb, "enabled", lambda: True)
    monkeypatch.setattr(radb, "indexed_file_ids", lambda owner: {"f1"})
    monkeypatch.setattr(radb, "errored_file_ids", lambda owner: set())
    files = [{"id": "f1", "filename": "spec.xlsx", "status": "completed"},
             {"id": "f2", "filename": "sheet2.xlsx", "status": "completed"},
             {"id": "f3", "filename": "policy.md", "status": "completed"}]
    out = rag_module.attach_backend_status("u", files)
    assert out[0]["backends"]["select_ai"] == "indexed"  # 索引に在る xlsx
    assert out[0]["backends"]["adb"] == "indexed"
    assert out[1]["backends"]["select_ai"] == "pending"  # まだ索引に無い xlsx = 同期待ち
    assert out[2]["backends"]["select_ai"] == "pending"  # md も同じ扱い


def test_opensearch_extracts_xlsx_instead_of_decoding_bytes():
    """xlsx をそのまま UTF-8 デコードして文字化け本文を投入しない。"""
    from jetuse_core import rag_opensearch

    text = rag_opensearch._extract_text("spec.xlsx", _workbook())
    assert "600 req/min" in text and "[制約 C5:E5]" in text
    assert "�" not in text


# --- PREP-03: スキャン PDF / 画像の結線 ----------------------------------------


@pytest.fixture
def scan(monkeypatch):
    """OCR をモックし、記憶をクリアした `extract_scan`(OCI は呼ばない)。"""
    from test_extract_scan import FakeOcr, build_pdf, build_png

    from jetuse_core import docunderstand, extract_scan

    fake = FakeOcr()
    monkeypatch.setattr(docunderstand, "ocr", fake)
    monkeypatch.setattr(docunderstand, "ocr_vlm", FakeOcr("VLM"))
    memos = (extract_scan._native, extract_scan._result, extract_scan._flags)
    for memo in memos:
        memo.clear()
    yield SimpleNamespace(ocr=fake, pdf=build_pdf, png=build_png())
    for memo in memos:
        memo.clear()


def test_extract_returns_page_numbers_for_a_scanned_pdf(scan):
    """層1(`POST /api/extract`)がスキャン PDF を通し、出典に頁が載る。"""
    res = client.post(
        "/api/extract", files={"file": ("scan.pdf", scan.pdf(["", ""]), "application/pdf")}
    )
    assert res.status_code == 200
    chunks = res.json()["chunks"]
    assert [c["sheet"] for c in chunks] == ["p.1", "p.2"]
    assert chunks[0]["text"] == "OCR1行目"


def test_extract_accepts_images(scan):
    res = client.post("/api/extract", files={"file": ("photo.png", scan.png, "image/png")})
    assert res.status_code == 200
    assert res.json()["chunks"][0]["sheet"] == "p.1"


def test_extract_does_not_ocr_a_pdf_that_has_a_text_layer(scan):
    """対照: 従来どおりテキスト層から取り、OCI を呼ばない(無駄な課金をしない)。"""
    res = client.post(
        "/api/extract",
        files={"file": ("born-digital.pdf", scan.pdf(["Rate limit 600 rpm"]), "x")},
    )
    assert res.status_code == 200
    assert "600 rpm" in res.json()["chunks"][0]["text"]
    assert scan.ocr.calls == []


def test_extract_rejects_an_unknown_ocr_engine_with_422(scan):
    res = client.post(
        "/api/extract", files={"file": ("scan.pdf", scan.pdf([""]), "x")},
        data={"ocr_engine": "vlmm"},
    )
    assert res.status_code == 422 and "vlmm" in res.json()["detail"]
    assert scan.ocr.calls == []


def test_extract_lets_the_caller_choose_the_vlm_engine(scan):
    res = client.post(
        "/api/extract", files={"file": ("scan.pdf", scan.pdf([""]), "x")},
        data={"ocr_engine": "vlm"},
    )
    assert res.status_code == 200
    assert res.json()["chunks"][0]["text"] == "VLM1行目"
    assert scan.ocr.calls == []          # DU は呼ばれない


def test_extract_rejects_over_the_page_limit_without_truncating(scan, monkeypatch):
    from jetuse_core import extract_scan

    monkeypatch.setattr(extract_scan, "MAX_OCR_PAGES", 1)
    res = client.post(
        "/api/extract", files={"file": ("scan.pdf", scan.pdf(["", ""]), "x")}
    )
    assert res.status_code == 422
    assert "limit=ocr_pages" in res.json()["detail"]
    assert scan.ocr.calls == []


def test_extract_reports_an_ocr_service_failure_as_503(scan, monkeypatch):
    """IAM 未整備は利用者の入力の問題ではない(422 にしない)。"""
    from jetuse_core import docunderstand

    def boom(content, **kw):
        raise docunderstand.OcrError("OCRサービスにアクセスできません(IAM未整備の可能性)")

    monkeypatch.setattr(docunderstand, "ocr", boom)
    res = client.post("/api/extract", files={"file": ("scan.pdf", scan.pdf([""]), "x")})
    assert res.status_code == 503 and "IAM" in res.json()["detail"]


def test_upload_accepts_images(scan, fake_rag):
    res = client.post("/api/rag/files", files={"file": ("photo.jpg", scan.png, "image/jpeg")})
    assert res.status_code == 200 and res.json()["filename"] == "photo.jpg"


def test_prepare_upload_converts_a_scan_to_text_with_the_page_range(scan):
    """マネージド Vector Store には OCR したテキストを渡す(属性はファイル単位)。"""
    from jetuse_core import rag as rag_module

    name, body, attrs = rag_module.prepare_upload("scan.pdf", scan.pdf(["", "", ""]))
    assert name == "scan.pdf.txt"
    assert body.decode().startswith("[p.1]\nOCR1行目")
    assert attrs == {"sheet": "p.1-p.3"}


def test_prepare_upload_passes_through_a_pdf_with_a_text_layer(scan):
    """テキスト層のある PDF は原本のまま渡す(従来どおり。変換も OCR もしない)。"""
    from jetuse_core import rag as rag_module

    pdf = scan.pdf(["Rate limit 600 rpm"])
    assert rag_module.prepare_upload("spec.pdf", pdf) == ("spec.pdf", pdf, {})
    assert scan.ocr.calls == []


def test_upload_ocrs_the_document_once_for_both_backends(scan, monkeypatch):
    """1 回のアップロードで OCR は 1 回(マネージド変換と ADB 取り込みで二重課金しない)。"""
    from jetuse_core import rag as rag_module
    from jetuse_core import rag_adb

    sent: dict = {}
    units: dict = {}
    monkeypatch.setattr(rag_module, "ensure_store", lambda owner: "vs_fake")
    monkeypatch.setattr(rag_module, "_backup_original", lambda *a: None)
    monkeypatch.setattr(rag_module, "_insert_file", lambda *a: None)
    monkeypatch.setattr(rag_module, "make_inference_client", lambda **kw: _FakeDp(sent))
    monkeypatch.setattr(rag_adb, "availability", lambda: rag_adb.READY)
    monkeypatch.setattr(
        rag_adb, "ingest",
        lambda o, fid, name, body, **kw: units.update(
            chunks=rag_adb.chunk_units(name, body, ocr_engine=kw.get("ocr_engine"))),
    )
    _use_real_add_file(monkeypatch)
    res = client.post(
        "/api/rag/files", files={"file": ("scan.pdf", scan.pdf(["", ""]), "application/pdf")}
    )
    assert res.status_code == 200
    assert len(scan.ocr.calls) == 1
    assert sent["upload_name"] == "scan.pdf.txt"
    assert sent["attributes"]["sheet"] == "p.1-p.2"
    assert [c["sheet"] for c in units["chunks"]] == ["p.1", "p.2"]   # 出典は頁ごと


def test_opensearch_ocrs_images_instead_of_indexing_mojibake(scan):
    """画像を UTF-8 デコードして化けた本文を "indexed" にしない。"""
    from jetuse_core import rag_opensearch

    assert rag_opensearch._extract_text("photo.png", scan.png) == "OCR1行目"


def test_upload_propagates_the_chosen_engine_to_every_backend(scan, monkeypatch):
    """`ocr_engine=vlm` は取り込み口からも効き、**全バックエンドが同じエンジンで読む**。

    片方だけ既定の DU に落ちると、同じファイルの本文がバックエンドごとに違う経路で
    起こされ、検索結果が選んだバックエンドで変わる（RAGM-04 で `kind` について直したのと
    同じ種類のズレ）。
    """
    from test_extract_scan import FakeOcr

    from jetuse_core import docunderstand, rag_adb, rag_opensearch
    from jetuse_core import rag as rag_module

    vlm = FakeOcr("VLM")
    monkeypatch.setattr(docunderstand, "ocr_vlm", vlm)
    sent: dict = {}
    seen: dict = {}
    monkeypatch.setattr(rag_module, "ensure_store", lambda owner: "vs_fake")
    monkeypatch.setattr(rag_module, "_backup_original", lambda *a: None)
    monkeypatch.setattr(rag_module, "_insert_file", lambda *a: None)
    monkeypatch.setattr(rag_module, "make_inference_client", lambda **kw: _FakeDp(sent))
    monkeypatch.setattr(rag_adb, "availability", lambda: rag_adb.READY)
    monkeypatch.setattr(
        rag_adb, "ingest",
        lambda o, fid, name, body, **kw: seen.update(
            adb=[c["sheet"] for c in rag_adb.chunk_units(name, body,
                                                         ocr_engine=kw.get("ocr_engine"))]),
    )
    monkeypatch.setattr(rag_opensearch, "enabled", lambda: True)
    monkeypatch.setattr(
        rag_opensearch, "ingest",
        lambda o, fid, name, body, **kw: seen.update(
            opensearch=rag_opensearch._extract_text(name, body,
                                                    ocr_engine=kw.get("ocr_engine"))),
    )
    _use_real_add_file(monkeypatch)
    res = client.post(
        "/api/rag/files",
        files={"file": ("scan.pdf", scan.pdf(["", ""]), "application/pdf")},
        data={"ocr_engine": "vlm"},
    )
    assert res.status_code == 200
    assert scan.ocr.calls == []                 # 既定の DU は呼ばれない
    assert len(vlm.calls) == 1                  # VLM が 1 回だけ（経路ごとに呼ばない）
    assert sent["upload_bytes"].decode().startswith("[p.1]\nVLM1行目")
    assert seen["adb"] == ["p.1", "p.2"]
    assert seen["opensearch"] == "VLM1行目\nVLM2行目"


def test_conflicting_attributes_are_rejected_before_paying_for_ocr(scan, monkeypatch):
    """どうせ 422 になる要求のために OCR（課金）を先に走らせない（review-4 PREP03-002）。"""
    _use_real_add_file(monkeypatch)
    for name, body in (("photo.png", scan.png), ("scan.pdf", scan.pdf([""]))):
        res = client.post(
            "/api/rag/files", files={"file": (name, body, "application/octet-stream")},
            data={"attributes": json.dumps({"sheet": "p.9"})},
        )
        assert res.status_code == 422, name
        assert "sheet" in res.json()["detail"]
    assert scan.ocr.calls == []      # OCI を 1 回も呼んでいない
