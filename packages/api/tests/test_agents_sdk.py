"""agent定義バリデーションの単体テスト（ADR-0009: hostedルーティング準拠）。

旧FW-01/01b のインプロセス Agents SDK エンジン（jetuse_core/agents_sdk.py）は
ADR-0009 で hosted コンテナへ置換済みのため削除。本ファイルは framework 定義の
バリデーション（422/非422）のみを検証する。"""

from fastapi.testclient import TestClient

from service.main import app

client = TestClient(app)

# ADR-0009: 旧framework値 'agents_sdk' は現行Literalから撤廃 → 'openai_agents' へ置換。
BASE = {"name": "t", "instructions": "i", "model": "gpt-oss-120b", "framework": "openai_agents"}


def test_agents_sdk_rejects_code_interpreter_only():
    """openai_agents(hostedコンテナ)はrag_search可、code_interpreterのみ422(ADR-0009)"""
    res = client.post("/api/agents", json={**BASE, "enabled_tools": ["code_interpreter"]})
    assert res.status_code == 422
    assert "code_interpreter" in res.json()["detail"]


def test_agents_sdk_allows_rag_search_in_definition():
    """rag_searchはhostedルーティングのバリデーションを通る(DB無し環境では503になるだけ)。
    422でないこと=validationを通過したこと、をDB非依存で確認する。"""
    res = client.post("/api/agents", json={**BASE, "enabled_tools": ["rag_search"]})
    assert res.status_code != 422


def test_langgraph_tools_definition_valid():
    """ADR-0009補正: 旧FW-02の『langgraphはツールありにauto_tools必須(422)』ルールは
    現行validated()(main.py:155-188)に存在しない。hostedルーティングでは承認フローは
    コンテナ側の責務であり、langgraph+tools+auto_tools=Falseは定義レベルで妥当。
    → 422でないことを検証(validation通過。DB未設定なら503になるが422ではない)。"""
    res = client.post("/api/agents", json={
        **BASE, "framework": "langgraph",
        "enabled_tools": ["web_search"], "auto_tools": False,
    })
    assert res.status_code != 422


def test_langgraph_rejects_mcp():
    res = client.post("/api/agents", json={
        **BASE, "framework": "langgraph", "mcp_server_ids": ["x"],
    })
    assert res.status_code == 422
