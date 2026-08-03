"""リージョンをまたぐ機能の可否を実測する(AGT-06 §A)。「動くはず」で書かないため。

対象: TTS(音声合成) / STT(到達性) / Document Understanding(OCR) / GenAI CP。
**リソースは作らない**(推論呼び出しと list 系のみ)。

実行:
  PYTHONPATH=spikes/agt06 .venv/bin/python spikes/agt06/probe_services.py [region ...]
"""

import base64
import json
import os
import sys

from _common import COMPARTMENT, err, png

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]
                       / "packages" / "api"))

import oci  # noqa: E402
from jetuse_core.oci_auth import sdk_signer_args  # noqa: E402

REGIONS = sys.argv[1:] or ["us-chicago-1", "ap-osaka-1"]


def probe(region: str) -> dict:
    rec: dict = {}
    args = sdk_signer_args(region)
    args["config"]["region"] = region

    # TTS(音声合成)。アプリ本体は TTS_REGION 未指定なら
    # 「デプロイリージョン → us-phoenix-1」の順に試す(FIX-58)ので、
    # ここが NG でも直ちにアプリが壊れるわけではない。
    try:
        c = oci.ai_speech.AIServiceSpeechClient(**args)
        resp = c.synthesize_speech(oci.ai_speech.models.SynthesizeSpeechDetails(
            text="テスト", compartment_id=COMPARTMENT,
            configuration=oci.ai_speech.models.TtsOracleConfiguration(
                model_family="ORACLE",
                model_details=oci.ai_speech.models.TtsOracleTts2NaturalModelDetails(
                    model_name="TTS_2_NATURAL", voice_id="Yuki", language_code="ja-JP"),
                speech_settings=oci.ai_speech.models.TtsOracleSpeechSettings(
                    output_format="MP3")),
        ))
        data = resp.data.content if hasattr(resp.data, "content") else resp.data.raw.read()
        rec["tts"] = {"ok": True, "bytes": len(data)}
    except Exception as e:  # noqa: BLE001
        rec["tts"] = err(e)

    # STT: ジョブは作らない(一覧が引けるか = 到達性)
    try:
        oci.ai_speech.AIServiceSpeechClient(**args).list_transcription_jobs(
            compartment_id=COMPARTMENT, limit=1)
        rec["stt_reachable"] = {"ok": True}
    except Exception as e:  # noqa: BLE001
        rec["stt_reachable"] = err(e)

    # Document Understanding(OCR)
    try:
        c = oci.ai_document.AIServiceDocumentClient(**args)
        c.analyze_document(oci.ai_document.models.AnalyzeDocumentDetails(
            features=[oci.ai_document.models.DocumentTextExtractionFeature(
                feature_type="TEXT_EXTRACTION")],
            document=oci.ai_document.models.InlineDocumentDetails(
                source="INLINE",
                data=base64.b64encode(png((255, 255, 255), 64)).decode()),
            compartment_id=COMPARTMENT, language="JPN"))
        rec["document_understanding"] = {"ok": True}
    except Exception as e:  # noqa: BLE001
        rec["document_understanding"] = err(e)

    # GenAI CP(project / Vector Store 本体の置き場)
    try:
        c = oci.generative_ai.GenerativeAiClient(**args)
        oci.pagination.list_call_get_all_results(c.list_generative_ai_projects, COMPARTMENT)
        rec["genai_cp"] = {"ok": True}
    except Exception as e:  # noqa: BLE001
        rec["genai_cp"] = err(e)
    return rec


out = {}
for region in REGIONS:
    out[region] = probe(region)
    print(region, json.dumps(out[region], ensure_ascii=False), flush=True)

with open(os.environ.get("PROBE_OUT", "probe_services.json"), "w") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
