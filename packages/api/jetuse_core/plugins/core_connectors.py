"""コアコネクタ・レジストリ（CON-03）。provider → 検証済みコネクタ定義/manifest の単一の引き当て。

`sample_app_registry`（コア同梱 sample-app）／`ai_runtime`（capability ハンドラ）と同方針で、
**コア同梱のコネクタ**を provider 名で引ける小さなレジストリを提供する。現状コアパレットは
Slack 1本（§6 D9。`slack_connector_builtin`）。これ以外は後段マーケット（S3+）で、合成段階では
パレット外。

本モジュールの責務は「束縛のための引き当て」に限定する:
  - `core_connector(provider)`: provider のコアコネクタ（無ければ None）。
  - `core_connector_providers()`: コアパレット（provider 名集合）。governance 許可パレットの正本。
  - `connector_invoke_scopes(defn)`: invoke に必要な Platform スコープ
    （`platform:connector.invoke` ＋ action 宣言スコープ。順序固定）。
  - `resolve_active_connector(comp, provider)`: 合成構成で **active** なコネクタの定義を返す
    （CON-02 `invoke_connector_action` へ渡し broker 経由で叩く）。

**実シークレットは一切持たない**（定義が持つのは secretRef = 参照名のみ。CON-01/02 の契約）。
副作用なし・決定的（DB/GenAI に触れない）。合成束縛は synth、デプロイ前ゲートは governance、
実呼び出しは connector_runtime（broker 強制）と関心を分離する。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from .connector import ConnectorDefinition, required_permissions
from .manifest import PLATFORM_SCOPE_CONNECTOR_INVOKE, PluginManifest
from .slack_connector_builtin import (
    slack_connector_definition,
    slack_connector_manifest,
)

if TYPE_CHECKING:  # 循環回避（synth は core_connectors を import するため、型のみ参照）
    from ..synth import DemoComposition


class CoreConnector:
    """コア同梱コネクタ 1 件への引き当て（検証済み定義/manifest のアクセサを束ねる）。

    アクセサ越しに参照するのは、定義/manifest が `@lru_cache` で 1 度だけ検証される正本だから
    （二重定義しない）。`provider` は安定キー（表示文言ではない）。
    """

    __slots__ = ("provider", "_definition_fn", "_manifest_fn")

    def __init__(
        self,
        provider: str,
        definition_fn: Callable[[], ConnectorDefinition],
        manifest_fn: Callable[[], PluginManifest],
    ) -> None:
        self.provider = provider
        self._definition_fn = definition_fn
        self._manifest_fn = manifest_fn

    def definition(self) -> ConnectorDefinition:
        """検証済みコネクタ定義（contributes["connector"]）。"""
        return self._definition_fn()

    def manifest(self) -> PluginManifest:
        """検証済みコネクタ manifest（kind=connector）。"""
        return self._manifest_fn()


#: provider → コアコネクタ。コアパレットは Slack 1本（§6 D9）。後段で Teams/Email 等を足すなら
#: ここに登録する（governance のパレットも自動で追従する＝二重定義しない）。
_CORE_CONNECTORS: dict[str, CoreConnector] = {
    "slack": CoreConnector(
        "slack", slack_connector_definition, slack_connector_manifest
    ),
}


def core_connector(provider: str) -> CoreConnector | None:
    """provider のコアコネクタを返す。コアパレット外（未知/後段マーケット）は None。"""
    return _CORE_CONNECTORS.get(provider)


def core_connector_providers() -> frozenset[str]:
    """コアコネクタ・パレット（provider 名の集合）。governance の許可パレットの正本。"""
    return frozenset(_CORE_CONNECTORS)


def connector_invoke_scopes(definition: ConnectorDefinition) -> list[str]:
    """この定義の任意 action を invoke するのに要求される Platform スコープ（順序固定）。

    コネクタを呼ぶ権利そのもの（`platform:connector.invoke`）を先頭に、action が宣言する
    Platform スコープ（和集合）を辞書順で続ける。connector_runtime._required_scopes（action 単位）の
    **コネクタ全体版**で、合成束縛時に「このコネクタを使うなら最低どのスコープが要る」を示すために使う。
    """
    scopes = [PLATFORM_SCOPE_CONNECTOR_INVOKE]
    for sc in sorted(required_permissions(definition)):
        if sc not in scopes:
            scopes.append(sc)
    return scopes


def resolve_active_connector(
    composition: DemoComposition, provider: str
) -> ConnectorDefinition | None:
    """合成構成で **active**（束縛済み・デプロイ可）なコネクタの定義を返す。無ければ None。

    呼び出し側（デモ実行・E2E）はこの定義を CON-02 の `invoke_connector_action` へ渡し、broker 経由
    （`platform:connector.invoke` 強制・監査）で実際に叩く。excluded（パレット外/合成不整合）や
    未束縛の provider には None を返す（fail-closed: 未束縛コネクタを invoke 経路に載せない）。
    """
    binding = next(
        (
            b
            for b in composition.connector_bindings
            if b.provider == provider and b.status == "active"
        ),
        None,
    )
    if binding is None:
        return None
    core = core_connector(provider)
    return core.definition() if core is not None else None
