"""コア同梱 sample-app のレジストリ(SBA-03)。

SBA-02 までは SBA-A 単体だったが、SBA-03 で SBA-B(在庫・受発注照会)が加わる。複数のコア同梱
sample-app を 1 箇所で束ね、ルート層(`service/routes/sample_apps.py`)が **どのアプリか** を意識
せずに「一覧 / 完全定義取得 / 実行時の定義・知識コーパス解決」を行えるようにする集約点。

各アプリの定義本体・seed・知識コーパスは個別モジュール(`sample_app_builtin*` )が持ち、本モジュールは
それらを `instance_id` で引けるよう登録するだけ(DB 非依存・副作用なし)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .sample_app import SampleAppDefinition
from .sample_app_builtin import (
    SBA_A_INSTANCE_ID,
    SBA_A_KNOWLEDGE_DATASET,
    sba_a_definition,
    sba_a_summary,
)
from .sample_app_builtin import (
    get_builtin_sample_app as _get_sba_a,
)
from .sample_app_builtin import (
    knowledge_corpus as _sba_a_corpus,
)
from .sample_app_builtin_sba_b import (
    SBA_B_INSTANCE_ID,
    sba_b_definition,
    sba_b_summary,
)
from .sample_app_builtin_sba_b import (
    get_sba_b_sample_app as _get_sba_b,
)


@dataclass(frozen=True)
class ResolvedApp:
    """実行時に必要な、検証済み定義と知識コーパス・知識データセット名。"""

    instance_id: str
    definition: SampleAppDefinition
    #: RAG/draft が根拠にする知識行(NL2SQL アプリでは空)。
    corpus: list[dict[str, Any]] = field(default_factory=list)
    #: 知識コーパスの元データセット名(無ければ None)。
    knowledge_dataset: str | None = None


def list_sample_apps() -> list[dict[str, Any]]:
    """全コア同梱 sample-app の一覧要約(home カード/実行導線用)。"""
    return [sba_a_summary(), sba_b_summary()]


def get_sample_app(app_id: str) -> dict[str, Any] | None:
    """app_id から完全定義(screens/datasets/aiSlots + seed + knowledge_dataset)を返す。"""
    return _get_sba_a(app_id) or _get_sba_b(app_id)


def resolve_app(app_id: str) -> ResolvedApp | None:
    """app_id から実行時の検証済み定義・知識コーパスを解決する(未知なら None)。"""
    if app_id == SBA_A_INSTANCE_ID:
        definition = sba_a_definition()
        return ResolvedApp(
            instance_id=SBA_A_INSTANCE_ID,
            definition=definition,
            corpus=_sba_a_corpus(definition),
            knowledge_dataset=SBA_A_KNOWLEDGE_DATASET,
        )
    if app_id == SBA_B_INSTANCE_ID:
        # SBA-B(NL2SQL)は知識コーパスを持たない。nl2sql/chart は定義の datasets と
        # 実行結果を文脈にするため corpus は空でよい。
        return ResolvedApp(
            instance_id=SBA_B_INSTANCE_ID,
            definition=sba_b_definition(),
            corpus=[],
            knowledge_dataset=None,
        )
    return None
