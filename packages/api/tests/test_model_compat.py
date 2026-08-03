"""モデル差の吸収層(AGT-06)。実機で観測した差だけを吸収する。

根拠(us-chicago-1 実測 — docs/verification/AGT-06.md):
- gemini 系は `role=system` の入力アイテムを 400 で拒否する
- gemini 系は stream=True のとき、返した `function_call` を `id` 付きで積み直すと
  400 `did not match any variant of untagged enum ResponseInput` になる(`id` を落とすと通る)
- gpt-oss / grok 系はどちらも受け付ける
"""

import pytest

from jetuse_core.model_compat import agent_refusal, responses_input
from jetuse_core.models import MODELS, ModelDef


def _msg(role: str, text: str) -> dict:
    return {"type": "message", "role": role, "content": [{"type": "input_text", "text": text}]}


PERMISSIVE = ModelDef("x.permissive", "responses", "P")
STRICT = ModelDef(
    "x.strict", "responses", "S", supports_system_role=False, echo_call_item_id=False
)


def test_permissive_model_input_is_untouched():
    items = [_msg("system", "指示"), _msg("user", "問い"),
             {"type": "function_call", "name": "t", "arguments": "{}",
              "call_id": "c1", "id": "fc_1"}]
    assert responses_input(PERMISSIVE, items) == items


def test_system_item_is_folded_into_user_without_changing_text():
    out = responses_input(STRICT, [_msg("system", "指示"), _msg("user", "問い")])
    assert [i["role"] for i in out] == ["user", "user"]
    # 内容は変えない(役割だけを移す)
    assert out[0]["content"] == [{"type": "input_text", "text": "指示"}]


def test_folding_preserves_position_of_trailing_system_item():
    """打ち切り時の force-answer は入力の**末尾**に付く。順序が入れ替わると効かない。"""
    items = [_msg("user", "問い"), _msg("system", "最終回答して")]
    out = responses_input(STRICT, items)
    assert out[-1]["role"] == "user"
    assert out[-1]["content"][0]["text"] == "最終回答して"


def test_call_item_id_is_dropped_for_strict_model():
    items = [{"type": "function_call", "name": "t", "arguments": '{"a":1}',
              "call_id": "c1", "id": "fc_1"}]
    out = responses_input(STRICT, items)
    assert out[0] == {"type": "function_call", "name": "t", "arguments": '{"a":1}',
                      "call_id": "c1"}


def test_call_id_is_kept_when_id_is_dropped():
    """`call_id` は function_call_output との対応付けに要る。落としてはならない。"""
    out = responses_input(STRICT, [{"type": "function_call", "call_id": "c1", "id": "fc_1"}])
    assert out[0]["call_id"] == "c1"


def test_other_item_types_are_untouched_by_id_stripping():
    """`id` を持つ他のアイテム(MCP承認要求など)まで削らない。"""
    items = [{"type": "mcp_approval_request", "id": "ap_1", "name": "t"}]
    assert responses_input(STRICT, items) == items


def test_input_is_not_mutated_in_place():
    """入力は往復のたびに使い回される。破壊的に書き換えると 2 ホップ目が壊れる。"""
    items = [_msg("system", "指示"),
             {"type": "function_call", "call_id": "c1", "id": "fc_1"}]
    before = [dict(i) for i in items]
    responses_input(STRICT, items)
    assert items == before


def test_agent_capable_model_is_not_refused():
    assert agent_refusal("gpt-oss-120b") is None


def test_agent_incapable_model_is_refused_with_reason():
    """黙って壊れるより断る。理由は利用者に見える文言で返す。"""
    reason = agent_refusal("grok-4.20-multi-agent")
    assert reason is not None
    assert MODELS["grok-4.20-multi-agent"].label in reason


def test_chat_family_model_is_refused_for_agent_mode():
    assert agent_refusal("llama-3.3-70b") is not None


def test_unknown_model_is_refused():
    assert agent_refusal("no-such-model") is not None


@pytest.mark.parametrize("key", sorted(MODELS))
def test_agent_capable_models_declare_no_block_reason(key: str):
    """`agent=False` と理由なしの組み合わせを禁じる(断るときは必ず理由を出す)。"""
    m = MODELS[key]
    assert m.agent is bool(not m.agent_blocked_reason)
    if m.agent:
        assert m.api == "responses", "agent=True は Responses 系のみ"
