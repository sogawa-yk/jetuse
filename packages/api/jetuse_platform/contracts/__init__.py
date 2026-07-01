"""Experience Builder MVP 契約スキーマのバリデータ。

スキーマ本体は本パッケージ同梱の `schemas/*.json`(spec 説明は
`specs/17-experience-builder/README.md`)。本パッケージは読み込み・検証のみ。
`import` 時にファイル IO はせず、`RUN_EVENT_TYPES` は初回アクセスで遅延評価する。
"""

from .loader import get_validator, load_schema
from .validators import (
    is_valid,
    run_event_types,
    validate_action_with_citations_config,
    validate_action_with_citations_event,
    validate_action_with_citations_input,
    validate_action_with_citations_output,
    validate_demo_bundle,
    validate_demo_evidence_pack,
    validate_experience,
    validate_run_event,
)


def __getattr__(name: str):  # PEP 562: RUN_EVENT_TYPES を遅延評価で公開
    if name == "RUN_EVENT_TYPES":
        return run_event_types()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "RUN_EVENT_TYPES",
    "get_validator",
    "is_valid",
    "load_schema",
    "run_event_types",
    "validate_action_with_citations_config",
    "validate_action_with_citations_event",
    "validate_action_with_citations_input",
    "validate_action_with_citations_output",
    "validate_demo_bundle",
    "validate_demo_evidence_pack",
    "validate_experience",
    "validate_run_event",
]
