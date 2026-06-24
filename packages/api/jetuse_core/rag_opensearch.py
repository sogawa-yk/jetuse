"""OpenSearch RAGバックエンド(ENH-05)。OCI Search with OpenSearch にチャンク+埋め込みを
index し、k-NNベクトル検索 → 取得チャンクをLLMに渡して回答する。

- 接続: settings.opensearch_endpoint (例 http://10.1.1.x:9200)。security_mode=DISABLED 前提で
  平文HTTP・認証なし(プライベートサブネット)。endpointが空なら無効。
- 埋め込み: cohere.embed-multilingual-v3.0(1024次元、embeddings.py)。
- index: 利用者ごと jetuse-rag-<ownerhash>。アップロード時に取り込み(rag.add_fileから best-effort)。
"""

import hashlib
import io
import logging
from typing import Any

import httpx

from .embeddings import EMBED_DIM, embed
from .genai import make_inference_client
from .settings import get_settings

logger = logging.getLogger("jetuse.rag_opensearch")

_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
_CHUNK_CHARS = 800
_CHUNK_OVERLAP = 120
_TOP_K = 5
GEN_MODEL = "meta.llama-3.3-70b-instruct"


def enabled() -> bool:
    return bool(get_settings().opensearch_endpoint)


def _base() -> str:
    ep = get_settings().opensearch_endpoint
    if not ep:
        raise RuntimeError("OpenSearch endpoint 未設定")
    return ep.rstrip("/")


def _index(owner: str) -> str:
    h = hashlib.sha1(owner.encode()).hexdigest()[:16]
    return f"jetuse-rag-{h}"


def _client() -> httpx.Client:
    # OCI OpenSearchは security_mode=DISABLED でも 9200 はTLS。証明書CNはFQDN/IPと
    # 一致しないことがあるため verify=False(プライベートサブネット内通信)。
    return httpx.Client(base_url=_base(), timeout=_TIMEOUT, verify=False)


# ---- テキスト抽出・チャンク ----

def _extract_text(filename: str, content: bytes) -> str:
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content))
        return "\n".join((p.extract_text() or "") for p in reader.pages)
    return content.decode("utf-8", errors="replace")


def _chunk(text: str) -> list[str]:
    text = " ".join(text.split())
    if not text:
        return []
    chunks: list[str] = []
    step = _CHUNK_CHARS - _CHUNK_OVERLAP
    for i in range(0, len(text), step):
        chunks.append(text[i:i + _CHUNK_CHARS])
        if i + _CHUNK_CHARS >= len(text):
            break
    return chunks


# ---- index 管理 ----

def ensure_index(owner: str) -> str:
    idx = _index(owner)
    with _client() as c:
        if c.head(f"/{idx}").status_code == 200:
            return idx
        body = {
            "settings": {"index": {"knn": True}},
            "mappings": {"properties": {
                "vector": {"type": "knn_vector", "dimension": EMBED_DIM,
                           "method": {"name": "hnsw", "space_type": "cosinesimil",
                                      "engine": "lucene"}},
                "text": {"type": "text"},
                "filename": {"type": "keyword"},
                "file_id": {"type": "keyword"},
                "chunk_no": {"type": "integer"},
            }},
        }
        r = c.put(f"/{idx}", json=body)
        if r.status_code >= 300 and "resource_already_exists" not in r.text:
            raise RuntimeError(f"index作成失敗: {r.status_code} {r.text[:200]}")
    return idx


def ingest(owner: str, file_id: str, filename: str, content: bytes) -> int:
    """1ファイルをチャンク+埋め込みしてindexに投入。投入チャンク数を返す。"""
    text = _extract_text(filename, content)
    chunks = _chunk(text)
    if not chunks:
        return 0
    vectors = embed(chunks, input_type="SEARCH_DOCUMENT")
    idx = ensure_index(owner)
    lines = []
    for n, (chunk, vec) in enumerate(zip(chunks, vectors, strict=True)):
        lines.append({"index": {"_index": idx, "_id": f"{file_id}-{n}"}})
        lines.append({"text": chunk, "vector": vec, "filename": filename,
                      "file_id": file_id, "chunk_no": n})
    ndjson = "\n".join(__import__("json").dumps(x, ensure_ascii=False) for x in lines) + "\n"
    with _client() as c:
        r = c.post("/_bulk?refresh=true", content=ndjson.encode("utf-8"),
                   headers={"Content-Type": "application/x-ndjson"})
        if r.status_code >= 300 or r.json().get("errors"):
            raise RuntimeError(f"bulk投入失敗: {r.status_code} {r.text[:300]}")
    return len(chunks)


def delete_file(owner: str, file_id: str) -> None:
    idx = _index(owner)
    with _client() as c:
        if c.head(f"/{idx}").status_code != 200:
            return
        c.post(f"/{idx}/_delete_by_query?refresh=true",
               json={"query": {"term": {"file_id": file_id}}})


def indexed_file_ids(owner: str) -> set[str]:
    """OpenSearch indexに取り込み済みの file_id 集合を返す(取り込みは同期=即時)。"""
    idx = _index(owner)
    try:
        with _client() as c:
            if c.head(f"/{idx}").status_code != 200:
                return set()
            r = c.post(f"/{idx}/_search", json={
                "size": 0,
                "aggs": {"fids": {"terms": {"field": "file_id", "size": 1000}}},
            })
            if r.status_code >= 300:
                return set()
            buckets = r.json().get("aggregations", {}).get("fids", {}).get("buckets", [])
            return {b["key"] for b in buckets}
    except Exception:
        logger.exception("opensearch indexed_file_ids failed (ignored)")
        return set()


# ---- 検索・生成 ----

def search(owner: str, query: str, k: int = _TOP_K) -> list[dict[str, Any]]:
    idx = _index(owner)
    qvec = embed([query], input_type="SEARCH_QUERY")[0]
    with _client() as c:
        if c.head(f"/{idx}").status_code != 200:
            return []
        r = c.post(f"/{idx}/_search", json={
            "size": k,
            "_source": ["text", "filename", "file_id"],
            "query": {"knn": {"vector": {"vector": qvec, "k": k}}},
        })
        if r.status_code >= 300:
            raise RuntimeError(f"検索失敗: {r.status_code} {r.text[:200]}")
        hits = r.json().get("hits", {}).get("hits", [])
    return [{"text": h["_source"]["text"], "filename": h["_source"].get("filename"),
             "file_id": h["_source"].get("file_id"), "score": h.get("_score")}
            for h in hits]


def generate(owner: str, prompt: str) -> tuple[str, list[dict[str, Any]]]:
    """k-NN検索した文脈でLLM回答を生成し、(本文, citations) を返す。

    rag_select_ai.generate と同一シグネチャ(main.pyのRAGディスパッチで共用)。
    """
    hits = search(owner, prompt)
    if not hits:
        return ("アップロードされた文書から関連する情報が見つかりませんでした。", [])
    context = "\n\n".join(f"[{i + 1}] ({h['filename']}) {h['text']}" for i, h in enumerate(hits))
    r = make_inference_client().chat.completions.create(
        model=GEN_MODEL,
        messages=[
            {"role": "system", "content":
             "あなたは文書アシスタントです。以下の参考文書のみを根拠に、"
             "日本語で簡潔に回答してください。参考文書にない事項は推測せず「不明」と述べること。"},
            {"role": "user", "content": f"参考文書:\n{context}\n\n質問: {prompt}"},
        ],
        temperature=0, max_tokens=1000,
    )
    answer = (r.choices[0].message.content or "").strip()
    # citations: ヒットしたファイルを重複排除(rag_select_aiと同形式)
    seen: set[str] = set()
    cites: list[dict[str, Any]] = []
    for h in hits:
        fn = h.get("filename") or ""
        if fn and fn not in seen:
            seen.add(fn)
            cites.append({"file_id": h.get("file_id") or fn, "filename": fn,
                          "score": h.get("score")})
    return answer, cites
