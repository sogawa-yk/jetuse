"""OCIマネージド・ガードレール(GAP-01)。

OCI Generative AIの ApplyGuardrails API(ネイティブ、オンデマンド対応)を使う。
実機調査(SPIKE-G1追補)の結論として、**マネージドで本アプリ(日本語主体)に有効なのは
プロンプトインジェクション検知のみ**を採用する:
- コンテンツモデレーション: 日本語非対応(`Language ja is not supported` 400) → 不採用
- PII: デフォルト設定で未検知 → 不採用
（これらは「OCIマネージドに無い/効かないため今回未実装」。docs/verification/GAP-01.md 参照）

ベストエフォート(失敗時はfail-open=通す。可用性優先)。
"""

import logging
import os

from .settings import get_settings

logger = logging.getLogger("jetuse.guardrails")

# 観測値は 0.0(無) / 1.0(有) の二値的。0.5でしきい
INJECTION_THRESHOLD = 0.5

_client = None


def _inference_client():
    global _client
    if _client is None:
        import oci

        region = get_settings().oci_region
        if os.environ.get("AUTH_MODE") == "resource_principal":
            signer = oci.auth.signers.get_resource_principals_signer()
            _client = oci.generative_ai_inference.GenerativeAiInferenceClient(
                {"region": region}, signer=signer
            )
        else:
            from .genai import load_local_oci_config

            _client = oci.generative_ai_inference.GenerativeAiInferenceClient(
                load_local_oci_config()
            )
    return _client


def check_prompt_injection(text: str) -> tuple[bool, float]:
    """(flagged, score) を返す。マネージドApplyGuardrailsのプロンプトインジェクション検知。

    判定不能(API失敗等)は fail-open: (False, 0.0)。
    """
    try:
        import oci.generative_ai_inference.models as m

        s = get_settings()
        r = _inference_client().apply_guardrails(
            apply_guardrails_details=m.ApplyGuardrailsDetails(
                compartment_id=s.compartment_ocid,
                guardrail_configs=m.GuardrailConfigs(prompt_injection_config={}),
                input=m.GuardrailsTextInput(type="TEXT", content=text[:8000]),
            )
        )
        pi = r.data.results.prompt_injection
        score = float(getattr(pi, "score", 0.0) or 0.0)
        return score >= INJECTION_THRESHOLD, score
    except Exception:
        logger.exception("prompt injection guardrail failed (pass-through)")
        return False, 0.0
