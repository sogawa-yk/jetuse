"""静的 Reference Implementation Catalog ローダー(MVP)。

実装方針 §5.1: 汎用 Catalog サービスは作らず、リポジトリ内の静的 Descriptor を
in-process で読むだけ。Descriptor 実体(`descriptors/*.json`)はパッケージ同梱
(`importlib.resources` で読込)。import 時には FS へ触れず、初回アクセスで遅延読込する。
公開 API はキャッシュ汚染を避けるため毎回ディープコピーを返す。
"""

from __future__ import annotations

import json
from copy import deepcopy
from functools import cache
from importlib.resources import files

from jetuse_platform.contracts import load_schema

# Descriptor のスキーマ参照キー → action 由来スキーマ基底に付く接尾辞。
# action `answer.with-citations@1` ↔ schema `answer-with-citations.{config,input,output,event}`。
_SCHEMA_SUFFIXES = {
    "configSchema": "config",
    "inputSchema": "input",
    "outputSchema": "output",
    "eventSchema": "event",
}

# MVP で既知の action(version 込み)。存在しない version/action を弾く。
_KNOWN_ACTIONS = frozenset({"answer.with-citations@1"})

# MVP で既知のシナリオ(supportedScenarios の妥当値)。
_KNOWN_SCENARIOS = frozenset({"support-answer-with-citations"})


def _index_descriptors(descriptors: list[dict]) -> dict[tuple[str, str], dict]:
    """(id, version) を鍵に索引化。重複は無警告上書きせず明示エラー。"""
    index: dict[tuple[str, str], dict] = {}
    for desc in descriptors:
        key = (desc["id"], desc["version"])
        if key in index:
            raise ValueError(f"duplicate descriptor (id, version)={key}")
        index[key] = desc
    return index


@cache
def _load_descriptors_cached() -> dict[tuple[str, str], dict]:
    """全 Descriptor を (id, version) 索引で読み込みキャッシュする(初回のみ IO・破壊厳禁)。

    走査順は決定化のためファイル名で sorted する。
    """
    descriptors_dir = files("jetuse_platform.reference_descriptors").joinpath("descriptors")
    descriptors = [
        json.loads(entry.read_text(encoding="utf-8"))
        for entry in sorted(descriptors_dir.iterdir(), key=lambda e: e.name)
        if entry.name.endswith(".json")
    ]
    return _index_descriptors(descriptors)


def list_capabilities() -> list[dict]:
    """登録済み Capability Descriptor を一覧で返す(各要素は独立コピー)。"""
    return [deepcopy(d) for d in _load_descriptors_cached().values()]


def get_capability(capability_id: str, version: str) -> dict:
    """id/version で 1 件返す。未知は KeyError(ルート側で 404 に変換)。"""
    desc = _load_descriptors_cached().get((capability_id, version))
    if desc is None:
        raise KeyError((capability_id, version))
    return deepcopy(desc)


def verify_descriptors() -> None:
    """各 Descriptor の自己整合を保証する(起動時 or テストで呼ぶ)。

    検証内容:
    - `action` がちょうど1つの `@` で `name@version` に分割でき(name/version とも非空)、
      MVP の既知 action 集合(`_KNOWN_ACTIONS`)に一致すること(存在しない version/action を弾く)。
    - `config/input/output/eventSchema` が EXB-01 に実在し(`load_schema`)、かつ
      `action` 由来のスキーマ基底(`<name>` の '.'→'-')と対応していること
      (= 存在しない/食い違う action へ変えたら検知できる)。
    - `supportedScenarios` が空でなく、既知シナリオのみであること。
    不整合は `ValueError`(スキーマ不在は `FileNotFoundError`)を送出する。
    """
    for desc in _load_descriptors_cached().values():
        action = desc.get("action")
        parts = action.split("@") if isinstance(action, str) else []
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ValueError(f"descriptor {desc.get('id')!r}: invalid action {action!r}")
        if action not in _KNOWN_ACTIONS:
            raise ValueError(f"descriptor {desc.get('id')!r}: unknown action {action!r}")
        base = parts[0].replace(".", "-")

        for key, suffix in _SCHEMA_SUFFIXES.items():
            ref = desc.get(key)
            load_schema(ref)  # 実在しなければ FileNotFoundError
            expected = f"{base}.{suffix}"
            if ref != expected:
                raise ValueError(
                    f"descriptor {desc['id']!r}: {key}={ref!r} は action {action!r} と"
                    f"不整合(期待 {expected!r})"
                )

        scenarios = desc.get("supportedScenarios")
        if not scenarios or not all(s in _KNOWN_SCENARIOS for s in scenarios):
            raise ValueError(
                f"descriptor {desc['id']!r}: supportedScenarios 不正 {scenarios!r}"
            )
