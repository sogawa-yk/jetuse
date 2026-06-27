"""コアコネクタ・レジストリ(CON-03)の単体テスト。

provider 引き当て・パレット導出・invoke スコープ算出・active コネクタ解決を、副作用なしで検証する。
"""

from jetuse_core.plugins.core_connectors import (
    connector_invoke_scopes,
    core_connector,
    core_connector_providers,
    resolve_active_connector,
)
from jetuse_core.plugins.manifest import PLATFORM_SCOPE_CONNECTOR_INVOKE
from jetuse_core.recommend import recommend
from jetuse_core.synth import synthesize


def _answers(**over):
    base = {
        "Q1": "support", "Q2": ["docs"], "Q3": "rag_qa",
        "Q4": "slack", "Q5": "chat_form", "Q6": "sample",
    }
    base.update(over)
    return base


def test_palette_is_slack_only():
    assert core_connector_providers() == frozenset({"slack"})
    assert core_connector("slack") is not None
    assert core_connector("teams") is None  # 後段マーケット(コア外)


def test_core_connector_exposes_validated_definition_and_manifest():
    core = core_connector("slack")
    assert core is not None
    defn = core.definition()
    man = core.manifest()
    assert defn.provider == "slack"
    assert defn.transport == "builtin"
    assert man.kind == "connector"


def test_invoke_scopes_lead_with_invoke_scope():
    defn = core_connector("slack").definition()
    scopes = connector_invoke_scopes(defn)
    # 呼ぶ権利そのものを先頭に、action 宣言スコープ(Slack は空)を続ける。
    assert scopes[0] == PLATFORM_SCOPE_CONNECTOR_INVOKE
    assert PLATFORM_SCOPE_CONNECTOR_INVOKE in scopes
    # 順序固定(決定的)。
    assert connector_invoke_scopes(defn) == scopes


def test_invoke_scopes_no_real_secret_leak():
    # 算出スコープに実シークレットが混入しない(参照名/スコープ語彙のみ)。
    scopes = connector_invoke_scopes(core_connector("slack").definition())
    assert all(s.startswith("platform:") for s in scopes)


def test_resolve_active_connector_returns_definition_for_active():
    comp = synthesize(recommend(_answers()))
    defn = resolve_active_connector(comp, "slack")
    assert defn is not None
    assert defn.provider == "slack"


def test_resolve_active_connector_none_for_unbound_provider():
    comp = synthesize(recommend(_answers()))
    # teams は束縛されていない(active でない) → invoke 経路に載せない(fail-closed)。
    assert resolve_active_connector(comp, "teams") is None


def test_resolve_active_connector_none_when_no_connectors():
    comp = synthesize(recommend(_answers(Q4="none")))
    assert comp.active_connectors == []
    assert resolve_active_connector(comp, "slack") is None
