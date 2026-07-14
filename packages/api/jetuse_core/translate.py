"""テキスト翻訳(ENH-10)。2方式を選択可能(SPIKE-E5で両方 大阪可用・低レイテンシ確認):

- llm: OCI Enterprise AI の高速モデル(llama-3.3-70b)で翻訳(既存generative-ai権限で動作)
- oci_language: OCI Language の batch翻訳(翻訳専用・最速)。
  CIのRPに `use ai-service-language-family` のIAMが必要

リアルタイム文字起こし(VOICE-02)の確定テキストを逐次翻訳して原文/訳文を併記する用途。
"""

import logging
import os

from .genai import make_inference_client
from .settings import get_settings

logger = logging.getLogger("jetuse.translate")

# UI提示用(コード, 表示名)。OCI Language/LLMとも対応
LANGUAGES = [
    {"code": "en", "label": "英語"}, {"code": "ja", "label": "日本語"},
    {"code": "zh", "label": "中国語"}, {"code": "ko", "label": "韓国語"},
    {"code": "es", "label": "スペイン語"}, {"code": "fr", "label": "フランス語"},
    {"code": "de", "label": "ドイツ語"},
]
_NAMES = {x["code"]: x["label"] for x in LANGUAGES}
_EN = {"en": "English", "ja": "Japanese", "zh": "Chinese", "ko": "Korean",
       "es": "Spanish", "fr": "French", "de": "German"}

_lang_client = None


def _oci_language_client():
    global _lang_client
    if _lang_client is None:
        import oci

        region = get_settings().oci_region
        if os.environ.get("AUTH_MODE") == "resource_principal":
            signer = oci.auth.signers.get_resource_principals_signer()
            _lang_client = oci.ai_language.AIServiceLanguageClient(
                {"region": region}, signer=signer
            )
        else:
            from .genai import load_local_oci_config

            _lang_client = oci.ai_language.AIServiceLanguageClient(load_local_oci_config())
    return _lang_client


def _via_llm(text: str, target: str) -> str:
    tgt = _EN.get(target, target)
    r = make_inference_client().chat.completions.create(
        model="meta.llama-3.3-70b-instruct",
        messages=[
            {"role": "system",
             "content": f"Translate the user's text into {tgt}. Output only the translation, "
                        "no notes or quotes."},
            {"role": "user", "content": text[:4000]},
        ],
        temperature=0, max_tokens=1000,
    )
    return (r.choices[0].message.content or "").strip()


def _via_oci_language(text: str, target: str, source: str | None) -> str:
    from oci.ai_language.models import BatchLanguageTranslationDetails, TextDocument

    doc = TextDocument(key="1", text=text[:4000], language_code=source or "ja")
    res = _oci_language_client().batch_language_translation(
        BatchLanguageTranslationDetails(
            documents=[doc], compartment_id=get_settings().compartment_ocid,
            target_language_code=target,
        )
    )
    docs = res.data.documents
    return docs[0].translated_text if docs else ""


def translate(text: str, target: str, *, source: str | None = None,
              backend: str = "llm") -> str:
    """textをtarget言語へ翻訳して返す。backend: llm | oci_language。

    oci_language が IAM未付与(404 NotAuthorizedOrNotFound)等で失敗した場合は
    LLM方式へフォールバックし、翻訳機能自体は壊さない(必要IAMはログに出す)。
    """
    text = (text or "").strip()
    if not text:
        return ""
    if backend == "oci_language":
        try:
            return _via_oci_language(text, target, source)
        except Exception as e:  # noqa: BLE001 — どんな失敗でも原文不達を避けLLMへ
            logger.warning(
                "oci_language translation failed (%s); falling back to llm. "
                "有効化には `Allow dynamic-group jetuse-dg to use "
                "ai-service-language-family in compartment jetuse-proto` が必要です。",
                e,
            )
            return _via_llm(text, target)
    return _via_llm(text, target)
