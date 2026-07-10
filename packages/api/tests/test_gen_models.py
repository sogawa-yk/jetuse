"""生成専用レジストリ(gen_models — SP3-06)と共用 MODELS の分離の検査。"""

from jetuse_core.gen_models import DEFAULT_GEN_MODEL, GEN_MODELS, GEN_MODELS_BY_OCI_ID
from jetuse_core.models import MODELS


def test_shared_models_registry_has_no_gpt5():
    # gpt-5 系はチャット UI へ漏らさない(自テナンシで 404 — tasks/SP3-06 作業内容2)
    assert not [k for k in MODELS if k.startswith("gpt-5")]
    assert not [d.oci_id for d in MODELS.values() if d.oci_id.startswith("openai.gpt-5.")]


def test_gen_registry_shape():
    assert DEFAULT_GEN_MODEL == "gpt-oss-120b"
    assert GEN_MODELS[DEFAULT_GEN_MODEL].shared is False  # 既定=自テナンシ(共有設定不要・後方互換)
    # 施主指定 7 モデル + 既定(tasks/SP3-06)。oci_id は一意(逆引き allowlist が崩れない)
    assert len(GEN_MODELS) == 8
    assert len(GEN_MODELS_BY_OCI_ID) == 8
    # api 種別: gpt-5 系は全て responses(chat 対応モデルも function tools は chat 不可 —
    # E2E 実測 2026-07-08「use /v1/responses instead」)。chat は自テナンシ 120b のみ
    assert {k for k, d in GEN_MODELS.items() if d.api == "chat"} == {"gpt-oss-120b"}
    # chicago 限定は 5.6 系のみ、他は大阪
    assert {k for k, d in GEN_MODELS.items() if d.region == "us-chicago-1"} == {
        "gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra"}
