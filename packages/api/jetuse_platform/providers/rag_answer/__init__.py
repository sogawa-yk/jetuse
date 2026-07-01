"""EXB-04: `answer.with-citations@1` (rag.answer) の RAG Provider Adapter。

実 RAG 実行を jetuse_core の実機検証済み RAG (`rag_select_ai` / `rag_opensearch` の
`generate`。既存 chat ルートの RAG ディスパッチと同一の委譲先) に委譲し、Stage 0 契約
(`answer-with-citations.*`) 準拠のイベント/出力へ整形する。OCI は直叩きせず、既存 RAG を
書き直さない (ADR-0021 seam / ADR-0024 委譲境界)。

## seam = 実装方針 §8.1 `CapabilityProvider`
Provider 契約の正本は §8.1 `CapabilityProvider(Protocol)`:
`descriptor` ＋ `async start(context, input) -> RunHandle` / `resume` / `cancel`。
同期/ストリーム/長時間ジョブの差は Provider 内部と Run イベントで吸収する。config(Builder が束縛)は
Provider 構築時に固定し、input(実行時) と分離する (§7.1)。RunContext/RunHandle は EXB-03 の Run/SSE
ルートが供給・消費する seam 型で、run_id 採番・seq/ts・SSE 化・run.started/completed/failed の写像は
EXB-03 の担当 (本タスク非ゴール)。base(feat/stage-1) にも feat/EXB-03 にも具象 Run seam は
未存在のため、本タスクで seam を定義し統合時に §8.1 と配線整合させる (tasks/EXB-04.md)。

## 認可境界 (fail-closed / ADR-0024・人間承認待ち)
`knowledge.space` を委譲先 `owner` に**無条件で写像しない**。既存 `/api/chat` は認証済み
`user.subject` を owner 境界にしているため、config で任意 space を指定できるだけの seam は
別 owner の索引を参照する認可迂回になる。よって:
- 認証主体 `principal` を RunContext から必須で受け取る。
- 共有/curated Knowledge (space != principal) は、アクセス確認済みの `resolve_owner` resolver が
  与えられたときだけ許可する (レジストリ実装と写像の承認は ADR-0024 の人間ゲート)。
- resolver 無しかつ space != principal は `PermissionError` で**拒否** (自分の Knowledge のみ)。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Generator, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Protocol

from jetuse_core import rag, rag_opensearch, rag_select_ai

from ...contracts.validators import (
    validate_action_with_citations_config,
    validate_action_with_citations_input,
    validate_action_with_citations_output,
)
from ...reference_descriptors import get_capability

# jetuse_core の RAG delegate: (owner, prompt, *, top_k) -> (answer, citations)。citation は
# {file_id,filename,score}。rag_select_ai / rag_opensearch の generate が同一シグネチャ
# (main.py の RAG ディスパッチで共用)。
RagGenerate = Callable[..., "tuple[str, list[dict[str, Any]]]"]

# (space, version, principal) -> owner。アクセス確認込みで KnowledgeSpace を委譲先 owner へ
# 解決する。EXB-03 / KnowledgeSpace レジストリが供給する (ADR-0024 承認後)。
ResolveOwner = Callable[[str, "str | None", str], str]

_BACKENDS: dict[str, RagGenerate] = {
    "select_ai": rag_select_ai.generate,
    "opensearch": rag_opensearch.generate,
}
# retrieval.topK を honor できる backend (それ以外に topK 指定が来たら黙殺せず拒否する)。
_TOPK_BACKENDS = frozenset({"opensearch"})
# 既定 backend は持たない (Codex EXB04-011)。どの backend が構成済みかは環境 (EXB-03/settings)
# が知る。select_ai(topK 非対応/ADB 必須) も opensearch(任意機能/cluster 必須) も無条件既定にすると
# 未構成環境で既定呼び出しが必ず失敗する。よって backend 指定か generate 注入を必須にする。

# ヒット無し(Empty)かつ backend が本文を返さない場合の既定文言。空 citations で正常終了する。
_EMPTY_ANSWER = "該当する情報が見つかりませんでした。"

_DELTA_CHARS = 200  # message.delta を逐次化する分割幅 (ponytail: 契約の「逐次 text」を最小充足)

_CAPABILITY_ID = "rag.answer"
_CAPABILITY_VERSION = "1.0.0"

Output = dict[str, Any]
Event = dict[str, Any]
EmitFn = Callable[[Event], Awaitable[None]]


@dataclass
class RunContext:
    """EXB-03 の Run/SSE ルートが供給する実行文脈 (§8.1)。

    run_id 採番・イベントの seq/ts 付与・SSE 化は EXB-03 側。本 Adapter は `emit` に
    Capability 固有イベント (retrieval.started / retrieval.completed / message.delta) を渡す。
    """

    run_id: str
    principal: str  # 認証主体 (認可境界。owner 解決に使う)
    emit: EmitFn
    resolve_owner: ResolveOwner | None = None
    cancelled: bool = False


@dataclass
class RunHandle:
    """Run の結果ハンドル (§8.1)。output は outputSchema 準拠 {answer, citations}。"""

    run_id: str
    status: str  # "completed" | "cancelled"
    output: Output | None = None


class CapabilityProvider(Protocol):
    """実装方針 §8.1 の Capability Provider seam。"""

    descriptor: Mapping[str, Any]

    async def start(self, context: RunContext, input: Mapping[str, Any]) -> RunHandle:
        ...

    async def resume(self, context: RunContext, user_input: Mapping[str, Any]) -> None:
        ...

    async def cancel(self, context: RunContext) -> None:
        ...


def _to_source(c: Mapping[str, Any]) -> str:
    """jetuse_core citation → 契約 source (引用元。例 doc#pN)。

    filename を優先し、無ければ file_id。page があれば `#pN` を付す (現状 backend は
    page を返さないため通常はファイル名のみ)。
    """
    name = str(c.get("filename") or c.get("file_id") or "").strip()
    page = c.get("page")
    return f"{name}#p{page}" if name and page else name


def _map_citations(cites: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """委譲先 citations を answer-with-citations の citation[] へ整形する。"""
    out: list[dict[str, Any]] = []
    for c in cites:
        source = _to_source(c)
        if not source:
            continue  # source は必須。空は落とす
        item: dict[str, Any] = {"source": source}
        score = c.get("score")
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            item["score"] = float(score)
        snippet = c.get("snippet") or c.get("text")
        if snippet:
            item["snippet"] = str(snippet)
        out.append(item)
    return out


def _chunks(text: str, size: int = _DELTA_CHARS) -> Generator[str, None, None]:
    for i in range(0, len(text), size):
        yield text[i : i + size]


class CoreRagAnswerProvider:
    """jetuse_core の generate 系に委譲する `rag.answer` の CapabilityProvider 実装。

    config は Builder が束縛する設定 (§7.1) として**構築時に固定**する。backend 指定か
    generate 注入が必須 (既定 backend を持たない)。
    """

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        backend: str | None = None,
        generate: RagGenerate | None = None,
        resolve_owner: ResolveOwner | None = None,
    ) -> None:
        # bind-time: Builder 束縛 config を契約 schema で検証 (信頼境界の入力検証)。
        validate_action_with_citations_config(config)
        # 既定 backend を持たない (fail-closed)。generate 注入か backend 明示を**ちょうど1つ**要求
        # (両指定は delegate/表示名/topK 可否が食い違う混成状態になるため拒否)。
        if (generate is None) == (backend is None):
            raise ValueError(
                f"specify exactly one of backend ({sorted(_BACKENDS)}) or a generate delegate"
            )
        if backend is not None and backend not in _BACKENDS:
            raise ValueError(f"unknown backend '{backend}'; choose {sorted(_BACKENDS)}")
        # 構築時に固定 (認可・設定境界)。呼び出し元の後続変更で space/version/topK が
        # 再検証なしに変わらないよう deep copy する。
        self._config = deepcopy(dict(config))
        self._resolve_owner = resolve_owner
        self._backend = backend or "(injected)"
        # backend 名から topK 可否を判定。delegate を直接注入した場合は topK を honor する前提。
        self._supports_topk = generate is not None or backend in _TOPK_BACKENDS
        self._generate = generate if generate is not None else _BACKENDS[backend]
        self.descriptor = get_capability(_CAPABILITY_ID, _CAPABILITY_VERSION)

    def _resolve_owner_for(self, context: RunContext) -> str:
        knowledge = self._config["knowledge"]
        space = str(knowledge["space"])
        version = knowledge.get("version")
        principal = context.principal
        resolver = context.resolve_owner or self._resolve_owner
        if resolver is not None:
            # アクセス確認込みの resolver に委譲 (version 対応も resolver 側で判断)。
            owner = resolver(space, version, principal)
            # fail-closed: 拒否を None/空で表す resolver 実装で "None"/空 owner を検索しない。
            if not isinstance(owner, str) or not owner.strip():
                raise PermissionError("resolve_owner returned no owner (access denied)")
            return owner
        # fail-closed 既定: 承認済み resolver 無し。自分の Knowledge のみ許可。
        if version is not None:
            raise ValueError(
                "knowledge.version pinning is unsupported without an approved "
                "resolver (ADR-0024 pending)"
            )
        if space != principal:
            raise PermissionError(
                f"shared KnowledgeSpace '{space}' requires an approved resolver "
                "(ADR-0024 pending); principal may only query its own space"
            )
        return principal

    def _call_delegate(self, owner: str, question: str, top_k: int | None):
        if top_k is None:
            return self._generate(owner, question)
        if not self._supports_topk:
            # 黙殺せず明示的に拒否 (指定した topK が効かないまま回答するのを防ぐ)。
            raise ValueError(
                f"retrieval.topK is unsupported by backend '{self._backend}'; "
                f"use a topK-capable backend {sorted(_TOPK_BACKENDS)}"
            )
        # topK は Builder 束縛 config 由来(§7.1 = 信頼値。実行時ユーザー入力ではない)であり、
        # backend 側(OpenSearch は index.max_result_window)が結果件数上限を持つため、
        # アプリ層で丸めず値どおり honor する (契約 configSchema は topK>=1・上限なし)。
        return self._generate(owner, question, top_k=top_k)

    async def start(
        self, context: RunContext, input: Mapping[str, Any]
    ) -> RunHandle:
        # 信頼境界の入力検証。input を契約 schema で弾く。
        validate_action_with_citations_input(input)
        if not context.principal:
            raise ValueError("principal (authenticated subject) is required")
        # 事前キャンセル: resolver(DB/ネットワークを引き得る)より前に確認し、無駄な処理を避ける。
        if context.cancelled:
            return RunHandle(context.run_id, "cancelled", None)
        # resolver は将来 DB/ネットワークを引く実装が注入され得るためスレッドへ退避
        # (イベントループを塞がない)。PermissionError/ValueError はそのまま伝播する。
        owner = await asyncio.to_thread(self._resolve_owner_for, context)
        top_k = (self._config.get("retrieval") or {}).get("topK")

        # resolver 後にも再確認 (その間の cancel を拾う)。
        if context.cancelled:
            return RunHandle(context.run_id, "cancelled", None)

        await context.emit({"type": "retrieval.started", "data": {}})

        # 実 RAG は jetuse_core に委譲 (OCI 直叩きしない)。blocking なのでスレッドへ退避
        # (イベントループを塞がない)。例外は上位(EXB-03)が run.failed へ写像する。
        # ponytail: to_thread は起動中スレッドを強制中断できない=実 backend 実行中の cancel は
        # 協調的(結果は破棄され得るが backend 呼び出し自体は完走)。真の中断は jetuse_core 側の
        # キャンセル対応が要る(本タスクでは既存 RAG を書き直さない=対象外。SEAM-NOTES に明記)。
        raw_answer, raw_cites = await asyncio.to_thread(
            self._call_delegate, owner, input["question"], top_k
        )
        if not isinstance(raw_answer, str):
            # delegate 契約は (str, list[dict])。0/False/bytes 等の壊れた戻り値を Empty にしない。
            raise RuntimeError(f"delegate returned non-str answer: {type(raw_answer).__name__}")
        if not isinstance(raw_cites, list) or not all(isinstance(c, dict) for c in raw_cites):
            raise RuntimeError("delegate returned non-list[dict] citations")
        # 既存 chat ルートと同じ後処理でファイル名を解決 (main 回帰パリティ)。
        # ADB を引く blocking 関数なのでスレッドへ退避 (イベントループを塞がない)。
        raw_cites = await asyncio.to_thread(
            rag.resolve_citation_filenames, owner, raw_cites
        )
        citations = _map_citations(raw_cites)
        # 委譲先が citation を返したのに source を1つも作れない = 壊れた出力。Empty と混同しない。
        if raw_cites and not citations:
            raise RuntimeError(
                "delegate returned citations but none had a usable source"
            )

        if context.cancelled:
            return RunHandle(context.run_id, "cancelled", None)

        await context.emit(
            {"type": "retrieval.completed", "data": {"citations": citations}}
        )

        answer_text = (raw_answer or "").strip()
        if not answer_text:
            # 真の Empty = citations も無い場合のみ定型文へ。citations があるのに本文が空なのは
            # 生成失敗/壊れた出力であり、Empty として隠さない。
            if citations:
                raise RuntimeError("delegate returned citations but an empty answer")
            answer_text = _EMPTY_ANSWER
        for piece in _chunks(answer_text):
            if context.cancelled:
                return RunHandle(context.run_id, "cancelled", None)
            await context.emit({"type": "message.delta", "data": {"text": piece}})
            await asyncio.sleep(0)  # イベントループへ制御を返す (途中 cancel を割り込める)

        output = {"answer": answer_text, "citations": citations}
        validate_action_with_citations_output(output)
        return RunHandle(context.run_id, "completed", output)

    async def resume(
        self, context: RunContext, user_input: Mapping[str, Any]
    ) -> None:
        # rag.answer は承認/中断ポイントを持たない単発 Capability。resume は非対応。
        raise NotImplementedError("rag.answer capability does not support resume")

    async def cancel(self, context: RunContext) -> None:
        context.cancelled = True


@dataclass
class _Collector:
    events: list[Event] = field(default_factory=list)

    async def emit(self, event: Event) -> None:
        self.events.append(event)


def drive(
    provider: CapabilityProvider,
    input: Mapping[str, Any],
    *,
    principal: str,
    resolve_owner: ResolveOwner | None = None,
    run_id: str = "run-local",
) -> tuple[list[Event], RunHandle]:
    """テスト/ローカル用の同期ドライバ。start() を回し (events, handle) を返す。

    EXB-03 の Run/SSE ルートは自前の RunContext.emit (SSE sink) で start() を駆動する。
    """
    collector = _Collector()
    context = RunContext(
        run_id=run_id, principal=principal, emit=collector.emit, resolve_owner=resolve_owner
    )
    handle = asyncio.run(provider.start(context, input))
    return collector.events, handle


__all__ = [
    "CapabilityProvider",
    "CoreRagAnswerProvider",
    "RagGenerate",
    "ResolveOwner",
    "RunContext",
    "RunHandle",
    "drive",
]
