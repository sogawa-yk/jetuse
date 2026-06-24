"""service層 validator のユニットテスト(P1c §5)。

route を経由せず `validate_agent_definition` / `validate_usecase_definition` を
直接検証する。422 セマンティクスは分割前の AgentDefinition.validated() と同一。
"""

import pytest
from fastapi import HTTPException

from service.schemas import AgentDefinition, UsecaseDefinition, UsecaseField


def _agent(**over):
    base = {"name": "a", "instructions": "i", "model": "gpt-oss-120b"}
    base.update(over)
    return AgentDefinition(**base)


def test_agent_unknown_model():
    with pytest.raises(HTTPException) as ei:
        _agent(model="nope").validated("owner")
    assert ei.value.status_code == 422
    assert "unknown model" in ei.value.detail


def test_agent_hosted_unsupported_tool():
    with pytest.raises(HTTPException) as ei:
        _agent(enabled_tools=["code_interpreter"]).validated("owner")
    assert ei.value.status_code == 422
    assert "do not support tools" in ei.value.detail


def test_agent_hosted_mcp_unsupported():
    with pytest.raises(HTTPException) as ei:
        _agent(mcp_server_ids=["x"]).validated("owner")
    assert ei.value.status_code == 422
    assert "MCP servers" in ei.value.detail


def test_agent_hosted_ok_returns_dump():
    out = _agent(enabled_tools=["web_search", "rag_search"]).validated("owner")
    assert out["model"] == "gpt-oss-120b"
    assert out["framework"] == "openai_agents"


def test_agent_select_ai_bad_tool():
    with pytest.raises(HTTPException) as ei:
        _agent(framework="select_ai", enabled_tools=["web_search"]).validated("owner")
    assert ei.value.status_code == 422
    assert "Select AI Agent unsupported tools" in ei.value.detail


def test_agent_select_ai_ok():
    out = _agent(framework="select_ai", enabled_tools=["sql", "rag"]).validated("owner")
    assert out["framework"] == "select_ai"


def _usecase(**over):
    base = {
        "name": "u",
        "fields": [UsecaseField(name="a", label="A")],
        "template": "{{a}}",
    }
    base.update(over)
    return UsecaseDefinition(**base)


def test_usecase_duplicate_fields():
    with pytest.raises(HTTPException) as ei:
        _usecase(
            fields=[UsecaseField(name="a", label="A"), UsecaseField(name="a", label="B")],
            template="{{a}}",
        ).validated()
    assert ei.value.status_code == 422
    assert "duplicate field names" in ei.value.detail


def test_usecase_select_needs_options():
    with pytest.raises(HTTPException) as ei:
        _usecase(
            fields=[UsecaseField(name="a", label="A", type="select")],
            template="{{a}}",
        ).validated()
    assert ei.value.status_code == 422
    assert "needs options" in ei.value.detail


def test_usecase_unknown_model():
    with pytest.raises(HTTPException) as ei:
        _usecase(model="nope").validated()
    assert ei.value.status_code == 422


def test_usecase_template_no_field():
    with pytest.raises(HTTPException) as ei:
        _usecase(template="no references").validated()
    assert ei.value.status_code == 422
    assert "template references no defined field" in ei.value.detail


def test_usecase_ok():
    out = _usecase().validated()
    assert out["template"] == "{{a}}"
