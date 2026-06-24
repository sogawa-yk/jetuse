"""OCI Generative AI のテキスト埋め込み(ENH-05)。OpenAI互換APIは /embeddings 非対応
(400 "Unsupported OpenAI operation")のため、ネイティブSDK embed_text を使う。

cohere.embed-multilingual-v3.0(1024次元、日本語対応。Select AI RAGと同一モデル)。
"""

import os

from .settings import get_settings

EMBED_MODEL = "cohere.embed-multilingual-v3.0"
EMBED_DIM = 1024
_BATCH = 96  # cohereの1リクエスト上限

_client = None


def _embed_client():
    global _client
    if _client is None:
        import oci
        from oci.generative_ai_inference import GenerativeAiInferenceClient

        region = get_settings().oci_region
        ep = f"https://inference.generativeai.{region}.oci.oraclecloud.com"
        if os.environ.get("AUTH_MODE") == "resource_principal":
            signer = oci.auth.signers.get_resource_principals_signer()
            _client = GenerativeAiInferenceClient({"region": region}, signer=signer,
                                                  service_endpoint=ep)
        else:
            _client = GenerativeAiInferenceClient(oci.config.from_file(), service_endpoint=ep)
    return _client


def embed(texts: list[str], *, input_type: str = "SEARCH_DOCUMENT") -> list[list[float]]:
    """テキスト群を埋め込みベクトルに変換する。input_typeは SEARCH_DOCUMENT / SEARCH_QUERY。"""
    from oci.generative_ai_inference.models import EmbedTextDetails, OnDemandServingMode

    if not texts:
        return []
    out: list[list[float]] = []
    comp = get_settings().compartment_ocid
    cli = _embed_client()
    for i in range(0, len(texts), _BATCH):
        batch = [t[:2000] for t in texts[i:i + _BATCH]]
        det = EmbedTextDetails(
            inputs=batch,
            serving_mode=OnDemandServingMode(model_id=EMBED_MODEL),
            compartment_id=comp,
            truncate="END",
            input_type=input_type,
        )
        out.extend(cli.embed_text(det).data.embeddings)
    return out
