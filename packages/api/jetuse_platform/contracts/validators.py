"""MVP 契約スキーマのバリデータ群。

各 `validate_*` は不正時に `jsonschema.ValidationError` を送出する(成功時は None)。
真偽値だけ欲しい場合は `is_valid` を使う。

標準 Run イベント語彙は schema(run-event)の enum を単一の正本とし、`run_event_types()`
で取得する。互換のため `RUN_EVENT_TYPES` 定数も公開するが、import 時に FS へ触れない
よう遅延評価(モジュール `__getattr__`)とする。
"""

from __future__ import annotations

from jsonschema import ValidationError

from .loader import get_validator, load_schema


def run_event_types() -> tuple[str, ...]:
    """標準 Run イベント語彙(実装方針 §7.4)。schema の enum を単一の真実源とする。"""
    return tuple(load_schema("run-event")["properties"]["type"]["enum"])


def __getattr__(name: str):  # PEP 562: RUN_EVENT_TYPES を遅延評価で公開
    if name == "RUN_EVENT_TYPES":
        return run_event_types()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _validate(schema_name: str, obj: object) -> None:
    get_validator(schema_name).validate(obj)


def is_valid(schema_name: str, obj: object) -> bool:
    return get_validator(schema_name).is_valid(obj)


def validate_experience(obj: object) -> None:
    _validate("experience", obj)


def validate_demo_bundle(obj: object) -> None:
    _validate("demo-bundle", obj)


def validate_demo_evidence_pack(obj: object) -> None:
    _validate("demo-evidence-pack", obj)


def validate_run_event(obj: object) -> None:
    _validate("run-event", obj)


def validate_action_with_citations_config(obj: object) -> None:
    _validate("answer-with-citations.config", obj)


def validate_action_with_citations_input(obj: object) -> None:
    _validate("answer-with-citations.input", obj)


def validate_action_with_citations_output(obj: object) -> None:
    _validate("answer-with-citations.output", obj)


def validate_action_with_citations_event(obj: object) -> None:
    _validate("answer-with-citations.event", obj)


__all__ = [
    "ValidationError",
    "is_valid",
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
