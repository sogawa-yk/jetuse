"""上流SSEの非JSONペイロード対策(2026-06-11 RAG実障害)。

OCIが一時エラーを単引用符dict等でストリームに流すとSDKがJSONDecodeErrorを
上げる。未出力ならリトライ、途中なら平易なメッセージで通知する。
"""

import json

import jetuse_core.chat as chat_mod
from jetuse_core.chat import stream_chat


def _decode_error():
    return json.JSONDecodeError("Expecting property name enclosed in double quotes", "{'", 1)


def test_parse_error_before_output_retries_once(monkeypatch):
    calls = {"n": 0}

    def fake_make_client(*a, **k):
        return object()

    def fake_stream(client, model, messages, temp, conv=None, params=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _decode_error()
        yield {"delta": "ok"}

    monkeypatch.setattr(chat_mod, "make_inference_client", fake_make_client)
    monkeypatch.setattr(chat_mod, "_stream_responses", fake_stream)
    events = list(stream_chat("gpt-oss-120b", [{"role": "user", "content": "q"}]))
    assert calls["n"] == 2  # 1回リトライ
    assert events == [{"delta": "ok"}]


def test_parse_error_mid_stream_yields_friendly_error(monkeypatch):
    def fake_make_client(*a, **k):
        return object()

    def fake_stream(client, model, messages, temp, conv=None, params=None):
        yield {"delta": "途中まで"}
        raise _decode_error()

    monkeypatch.setattr(chat_mod, "make_inference_client", fake_make_client)
    monkeypatch.setattr(chat_mod, "_stream_responses", fake_stream)
    events = list(stream_chat("gpt-oss-120b", [{"role": "user", "content": "q"}]))
    assert events[0] == {"delta": "途中まで"}
    assert "解析に失敗" in events[1]["error"]
    assert "double quotes" not in events[1]["error"]  # 生のJSONエラーを見せない
