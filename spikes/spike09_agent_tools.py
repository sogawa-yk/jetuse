"""SPIKE-09: Responses APIのツール機構検証（AGT-01前提）。

①カスタムfunctionツール: 定義の受理、function_callイベント形式、
  function_call_outputの提出と最終回答
②code_interpreter built-in
③web_search built-in
実行: .venv/bin/python spikes/spike09_agent_tools.py
"""

import json
import sys

sys.path.insert(0, "packages/api")

from jetuse_core.chat import make_inference_client  # noqa: E402
from jetuse_core.models import MODELS  # noqa: E402

MODEL = MODELS["gpt-oss-120b"].oci_id
client = make_inference_client(with_project=True)

WEATHER_TOOL = {
    "type": "function",
    "name": "get_weather",
    "description": "指定した都市の現在の天気を返す",
    "parameters": {
        "type": "object",
        "properties": {"city": {"type": "string", "description": "都市名"}},
        "required": ["city"],
    },
}


def section(t):
    print(f"\n{'=' * 8} {t} {'=' * 8}")


def dump_items(resp):
    for item in resp.output:
        t = getattr(item, "type", "?")
        if t == "function_call":
            print(f"  [function_call] name={item.name} args={item.arguments} "
                  f"call_id={getattr(item, 'call_id', None)}")
        elif t == "message":
            print(f"  [message] {resp.output_text[:150]}")
        else:
            print(f"  [{t}]")


def test_function_calling():
    section("1. function calling (非ストリーミング)")
    r = client.responses.create(
        model=MODEL,
        input="大阪の天気を教えて",
        tools=[WEATHER_TOOL],
        store=False,
    )
    dump_items(r)
    call = next((i for i in r.output if getattr(i, "type", "") == "function_call"), None)
    if not call:
        print("  -> function_callが返らない")
        return
    section("1b. function_call_output提出 → 最終回答")
    r2 = client.responses.create(
        model=MODEL,
        input=[
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "大阪の天気を教えて"}]},
            call.model_dump(exclude_none=True),
            {"type": "function_call_output", "call_id": call.call_id,
             "output": json.dumps({"weather": "晴れ", "temp_c": 24})},
        ],
        tools=[WEATHER_TOOL],
        store=False,
    )
    print("  final:", (r2.output_text or "")[:200])

    section("1c. function calling ストリーミングイベント形式")
    stream = client.responses.create(
        model=MODEL, input="東京の天気は?", tools=[WEATHER_TOOL], store=False, stream=True,
    )
    types = []
    for ev in stream:
        et = getattr(ev, "type", "?")
        if et not in types:
            types.append(et)
        if et == "response.output_item.done":
            item = ev.item
            if getattr(item, "type", "") == "function_call":
                print(f"  [item.done] function_call name={item.name} args={item.arguments}")
    print("  event types:", types)


def test_builtin(tool_def, label, prompt):
    section(label)
    try:
        r = client.responses.create(
            model=MODEL, input=prompt, tools=[tool_def], store=False,
        )
        for item in r.output:
            print(f"  [{getattr(item, 'type', '?')}]")
        print("  text:", (r.output_text or "")[:200])
    except Exception as e:
        print(f"  NG: {type(e).__name__}: {str(e)[:300]}")


if __name__ == "__main__":
    test_function_calling()
    test_builtin({"type": "code_interpreter", "container": {"type": "auto"}},
                 "2. code_interpreter built-in", "1から100までの素数の個数をPythonで計算して")
    test_builtin({"type": "web_search"}, "3. web_search built-in",
                 "OCIの最新リージョン数を調べて")
