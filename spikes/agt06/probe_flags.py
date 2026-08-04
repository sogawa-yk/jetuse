"""登録簿の各フラグ(reasoning / vision / multi_image)を実機で裏取りする(AGT-06)。

`ModelDef` のフラグは「動くはず」で書かない。ここで ok にならなかったものは false にする。
名前からの推測は当てにならない: `xai.grok-4.20-reasoning` は名前に反して
`reasoning effort` を **400 で拒否する**(実測)。

実行:
  PYTHONPATH=spikes/agt06 .venv/bin/python spikes/agt06/probe_flags.py [oci-model-id ...]
"""

import json
import os
import sys

from _common import REGION, client, data_url, err, msg, png

rc = client(with_project=True)
RED, BLUE = png((255, 0, 0)), png((0, 0, 255))

DEFAULT_MODELS = [
    "openai.gpt-oss-120b", "openai.gpt-oss-20b",
    "xai.grok-4.3", "xai.grok-4.20-reasoning", "xai.grok-4.20-non-reasoning",
    "google.gemini-2.5-pro", "google.gemini-2.5-flash", "google.gemini-2.5-flash-lite",
]


def probe(model: str) -> dict:
    rec: dict = {"model": model, "region": REGION}

    # reasoning effort を受け付けるか(ModelDef.reasoning)
    try:
        r = rc.responses.create(model=model, input=[msg("user", "1+1は？数字だけ。")],
                                reasoning={"effort": "low"}, max_output_tokens=2048,
                                store=False)
        rec["reasoning"] = {"ok": True, "text": (r.output_text or "")[:40]}
    except Exception as e:  # noqa: BLE001
        rec["reasoning"] = err(e)

    # 画像入力(ModelDef.vision / multi_image)。32px 以上で測ること(_common.png 参照)
    for flag, imgs, q in (("vision", [RED], "画像は何色？色名だけ答えて。"),
                          ("multi_image", [RED, BLUE],
                           "画像は2枚ある。それぞれ何色？色名だけ2つ答えて。")):
        content = [{"type": "input_text", "text": q}]
        content += [{"type": "input_image", "image_url": data_url(b)} for b in imgs]
        try:
            r = rc.responses.create(
                model=model, input=[{"type": "message", "role": "user", "content": content}],
                max_output_tokens=2048, store=False)
            rec[flag] = {"ok": True, "text": (r.output_text or "")[:60]}
        except Exception as e:  # noqa: BLE001
            rec[flag] = err(e)
    return rec


out = []
for m in sys.argv[1:] or DEFAULT_MODELS:
    rec = probe(m)
    out.append(rec)
    print(json.dumps(rec, ensure_ascii=False), flush=True)

with open(os.environ.get("PROBE_OUT", "probe_flags.json"), "w") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
