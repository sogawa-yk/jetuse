"""モデルのエージェント適性を実測する(AGT-06)。登録簿を増やす前にこれを回す。

測る軸:
  responses  : Responses API に到達できるか(404 でないか)
  system     : type=message role=system の入力アイテムを受け付けるか
  tools      : 入れ子引数つき function tool を呼べるか
  roundtrip  : function_call + function_call_output を積み直した続きの往復が通るか
               (**stream=True で**。`id` を含めた形と外した形の両方を測る —
                gemini はストリーミングかつ id 付きのときだけ 400 になる)

実行:
  PYTHONPATH=spikes/agt06 .venv/bin/python spikes/agt06/probe_caps.py [oci-model-id ...]
  PROBE_OUT=out.json AGT06_PROBE_REGION=ap-osaka-1 \
    PYTHONPATH=spikes/agt06 .venv/bin/python spikes/agt06/probe_caps.py
"""

import json
import os
import sys
import time

from _common import NESTED_ASK, NESTED_TOOL, REGION, client, err, msg

rc = client(with_project=True)

DEFAULT_MODELS = [
    "openai.gpt-oss-120b", "openai.gpt-oss-20b",
    "google.gemini-2.5-pro", "google.gemini-2.5-flash", "google.gemini-2.5-flash-lite",
    "xai.grok-4.3", "xai.grok-4.20-reasoning", "xai.grok-4.20-non-reasoning",
    "xai.grok-4.20-multi-agent",
    "meta.llama-4-maverick-17b-128e-instruct-fp8", "meta.llama-4-scout-17b-16e-instruct",
    "meta.llama-3.3-70b-instruct",
    "cohere.command-a-03-2025", "cohere.command-latest",
]


def _first_call(model: str, stream: bool):
    """1 ホップ目を回し、chat.py と同じ形の call dict(id 込み)を返す。"""
    if not stream:
        r = rc.responses.create(model=model, input=[msg("user", NESTED_ASK)],
                                tools=[NESTED_TOOL], store=False, max_output_tokens=2048)
        calls = [o for o in (r.output or []) if getattr(o, "type", "") == "function_call"]
    else:
        s = rc.responses.create(model=model, input=[msg("user", NESTED_ASK)],
                                tools=[NESTED_TOOL], stream=True, store=False,
                                max_output_tokens=2048)
        calls = []
        try:
            for ev in s:
                if getattr(ev, "type", "") == "response.output_item.done" \
                        and getattr(ev.item, "type", "") == "function_call":
                    calls.append(ev.item)
        finally:
            s.close()
    if not calls:
        return None
    d = calls[0].model_dump(exclude_none=True)
    return {k: v for k, v in d.items() if k in ("type", "name", "arguments", "call_id", "id")}


def _roundtrip(model: str, call: dict, stream: bool) -> str:
    inp = [msg("user", NESTED_ASK), call,
           {"type": "function_call_output", "call_id": call.get("call_id"),
            "output": '{"status":"accepted","order_id":"ORD-001"}'}]
    if not stream:
        r = rc.responses.create(model=model, input=inp, tools=[NESTED_TOOL], store=False,
                                max_output_tokens=2048)
        return (r.output_text or "")[:100]
    s = rc.responses.create(model=model, input=inp, tools=[NESTED_TOOL], stream=True,
                            store=False, max_output_tokens=2048)
    text = ""
    try:
        for ev in s:
            if getattr(ev, "type", "") == "response.output_text.delta":
                text += ev.delta
    finally:
        s.close()
    return text[:100]


def probe(model: str) -> dict:
    rec: dict = {"model": model, "region": REGION}

    for key, items in (("system", [msg("system", "あなたは簡潔な助手。"),
                                   msg("user", "1+1は？数字だけ答えて。")]),
                       ("responses", [msg("user", "1+1は？数字だけ答えて。")])):
        try:
            r = rc.responses.create(model=model, input=items, max_output_tokens=2048,
                                    store=False)
            rec[key] = {"ok": True, "text": (r.output_text or "")[:60]}
        except Exception as e:  # noqa: BLE001
            rec[key] = err(e)

    if not rec["responses"]["ok"]:
        return rec  # Responses に到達できない = エージェント不可

    try:
        call = _first_call(model, stream=True)
        if call is None:
            rec["tools"] = {"ok": False, "type": "no_function_call"}
            return rec
        args = json.loads(call.get("arguments") or "{}")
        rec["tools"] = {"ok": True, "arguments": call.get("arguments"),
                        "nested_ok": isinstance(args.get("opts"), dict)}
    except Exception as e:  # noqa: BLE001
        rec["tools"] = err(e)
        return rec

    for label, c in (("roundtrip_with_id", call),
                     ("roundtrip_no_id", {k: v for k, v in call.items() if k != "id"})):
        if label == "roundtrip_with_id" and "id" not in call:
            rec[label] = {"skipped": "モデルが id を返さない"}
            continue
        try:
            rec[label] = {"ok": True, "text": _roundtrip(model, c, stream=True)}
        except Exception as e:  # noqa: BLE001
            rec[label] = err(e)
    return rec


out = []
for m in sys.argv[1:] or DEFAULT_MODELS:
    t0 = time.time()
    rec = probe(m)
    rec["elapsed_s"] = round(time.time() - t0, 1)
    out.append(rec)
    print(json.dumps(rec, ensure_ascii=False), flush=True)

with open(os.environ.get("PROBE_OUT", "probe_caps.json"), "w") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
