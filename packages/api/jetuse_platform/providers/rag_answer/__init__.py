"""EXB-04: `answer.with-citations@1` (rag.answer) の実 RAG Provider (EXB-03 RunProvider seam)。

実 RAG を jetuse_core の実機検証済み RAG (`rag_select_ai` / `rag_opensearch` の `generate`。
ルートと同一の委譲先) に委譲し、EXB-03 の Run Engine (`service/runs.py`) の `RunProvider` 契約
`run(ctx) -> Iterator[dict]` を満たす。engine が lifecycle (run.started/completed/failed)・出力組立
(delta 累積→answer, retrieval→citations)・イベント順序検証を担うので、本 Provider は capability 固有
イベント (retrieval.started / retrieval.completed / message.delta) の dict を yield するだけ。OCI は
直叩きせず、既存 RAG を書き直さない (ADR-0021 / ADR-0024)。

§8.1 `CapabilityProvider` は概念契約で、EXB-03 が sync `RunProvider.run(ctx)` として実体化した。
本 Provider はその実体に適合する (engine が lifecycle / cancel / 出力を所有)。

## 認可境界 (ADR-0024 Accepted・施主承認 2026-07-01)
- principal = `ctx.owner_sub`。config = `ctx.config` (Experience 束縛。MVP 未束縛時は None)。
- config 未束縛時は**自分の Knowledge** (`space = owner_sub`) = 既存 `/api/chat` (owner=subject) と
  同一の spec 準拠動作。
- 共有/curated Knowledge (`space != principal`) は**アクセス確認済みの `resolve_owner` が注入された
  ときだけ**許可 (ADR-0024 決定1)。未注入は `PermissionError` で拒否 (fail-closed)。
- `retrieval.topK` は施主承認の契約上限 (configSchema `maximum` = `_MAX_TOPK`) を Provider/backend
  境界でも明示エラーで拒否する (暗黙クランプなし。ADR-0024 決定3 の施主上書き / EXB04-042)。
"""

from __future__ import annotations

from collections.abc import Callable, Generator, Iterator, Mapping
from typing import Any

from jetuse_core import rag, rag_opensearch, rag_select_ai

from ...contracts.validators import validate_action_with_citations_config

# jetuse_core の RAG delegate: (owner, prompt, *, top_k) -> (answer, citations)。citation は
# {file_id,filename,score}。rag_select_ai / rag_opensearch の generate が同一シグネチャ。
RagGenerate = Callable[..., "tuple[str, list[dict[str, Any]]]"]

# (space, version, principal) -> owner。アクセス確認込みで KnowledgeSpace を委譲先 owner へ解決する
# (承認済みレジストリが供給。ADR-0024 決定1)。
ResolveOwner = Callable[[str, "str | None", str], str]

_BACKENDS: dict[str, RagGenerate] = {
    "select_ai": rag_select_ai.generate,
    "opensearch": rag_opensearch.generate,
}
# retrieval.topK を honor できる backend (それ以外に topK 指定が来たら黙殺せず拒否する)。
_TOPK_BACKENDS = frozenset({"opensearch"})
# topK 上限 (施主承認: configSchema の maximum と同値)。Provider/backend 境界でも超過を明示拒否。
_MAX_TOPK = 100

# ヒット無し(Empty)かつ backend が本文を返さない場合の既定文言。空 citations で正常終了する。
_EMPTY_ANSWER = "該当する情報が見つかりませんでした。"

_DELTA_CHARS = 200  # message.delta を逐次化する分割幅 (契約の「逐次 text」を最小充足)

Event = dict[str, Any]


def _to_source(c: Mapping[str, Any]) -> str:
    """jetuse_core citation → 契約 source (引用元。例 doc#pN)。

    filename 優先、無ければ file_id。page があれば `#pN` (現状 backend は page を返さない)。
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
    """jetuse_core の generate 系に委譲する `rag.answer` の RunProvider 実装。

    backend 指定 (`select_ai`/`opensearch`) か generate 注入の**ちょうど1つ**が必須 (既定 backend を
    持たない。どの backend が構成済みかは環境=EXB-03/settings が決める)。config は実行ごとに
    `ctx.config` から取り、未束縛なら自分の Knowledge を引く。
    """

    def __init__(
        self,
        *,
        backend: str | None = None,
        generate: RagGenerate | None = None,
        resolve_owner: ResolveOwner | None = None,
    ) -> None:
        if (generate is None) == (backend is None):
            raise ValueError(
                f"specify exactly one of backend ({sorted(_BACKENDS)}) or a generate delegate"
            )
        if backend is not None and backend not in _BACKENDS:
            raise ValueError(f"unknown backend '{backend}'; choose {sorted(_BACKENDS)}")
        self._resolve_owner_fn = resolve_owner
        self._backend = backend or "(injected)"
        # backend 名から topK 可否を判定。delegate を直接注入した場合は topK を honor する前提。
        self._supports_topk = generate is not None or backend in _TOPK_BACKENDS
        self._generate = generate if generate is not None else _BACKENDS[backend]

    def _resolve_owner(self, config: Mapping[str, Any], principal: str) -> str:
        knowledge = config["knowledge"]
        space = str(knowledge["space"])
        version = knowledge.get("version")
        if self._resolve_owner_fn is not None:
            # アクセス確認済みの resolver に委譲 (version 対応も resolver 側で判断)。
            owner = self._resolve_owner_fn(space, version, principal)
            # fail-closed: 拒否を None/空で表す resolver 実装で "None"/空 owner を検索しない。
            if not isinstance(owner, str) or not owner.strip():
                raise PermissionError("resolve_owner returned no owner (access denied)")
            return owner
        # fail-closed 既定: 承認済み resolver 無し。自分の Knowledge のみ許可。
        if version is not None:
            raise ValueError(
                "knowledge.version pinning requires an approved resolver (ADR-0024)"
            )
        if space != principal:
            raise PermissionError(
                f"shared KnowledgeSpace '{space}' requires an approved resolver; "
                "principal may only query its own space"
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
        if top_k > _MAX_TOPK:
            # 施主承認の契約上限。暗黙クランプせず明示エラーで拒否する。
            raise ValueError(f"retrieval.topK {top_k} exceeds max {_MAX_TOPK}")
        return self._generate(owner, question, top_k=top_k)

    def run(self, ctx: Any) -> Iterator[Event]:
        """EXB-03 RunProvider seam。capability 固有イベント dict を yield する。

        engine が lifecycle・出力組立・イベント順序検証・(将来の)cancel を所有する。本ジェネレータは
        yield 境界で engine に制御を返すため、Run キャンセル実装時はここで協調的に停止できる。
        ブロッキング委譲 (下記 generate) の即時中断はコア RAG 非改変によりスコープ外 (residual)。
        """
        principal = ctx.owner_sub
        if not principal:
            raise ValueError("owner_sub (authenticated subject) is required")
        # config は Experience 束縛 (ctx.config)。**未束縛(None)** のときだけ自分の Knowledge を
        # 既定にする。明示束縛の空 dict {} は壊れた Experience 設定なので既定で隠さず schema で弾く
        # (None と {} を区別する。Codex EXB04-047)。
        config = ctx.config if ctx.config is not None else {"knowledge": {"space": principal}}
        validate_action_with_citations_config(config)  # 束縛設定を契約 schema で検証
        owner = self._resolve_owner(config, principal)
        top_k = (config.get("retrieval") or {}).get("topK")
        question = ctx.input["question"]

        yield {"type": "retrieval.started", "data": {}}

        # 実 RAG は jetuse_core に委譲 (OCI 直叩きしない)。engine は worker thread で本 gen を
        # 回すので同期委譲でよい。例外は engine が run.failed へ写像する。
        raw_answer, raw_cites = self._call_delegate(owner, question, top_k)
        if not isinstance(raw_answer, str):
            # delegate 契約は (str, list[dict])。壊れた戻り値を Empty として通さない。
            raise RuntimeError(f"delegate returned non-str answer: {type(raw_answer).__name__}")
        if not isinstance(raw_cites, list) or not all(isinstance(c, dict) for c in raw_cites):
            raise RuntimeError("delegate returned non-list[dict] citations")
        # 既存 chat ルートと同じ後処理でファイル名を解決 (main 回帰パリティ)。
        raw_cites = rag.resolve_citation_filenames(owner, raw_cites)
        citations = _map_citations(raw_cites)
        if raw_cites and not citations:
            raise RuntimeError("delegate returned citations but none had a usable source")

        yield {"type": "retrieval.completed", "data": {"citations": citations}}

        answer_text = raw_answer.strip()
        if not answer_text:
            # 真の Empty = citations も無い場合のみ定型文へ。citations があるのに本文が空なのは
            # 生成失敗/壊れた出力であり、Empty として隠さない。
            if citations:
                raise RuntimeError("delegate returned citations but an empty answer")
            answer_text = _EMPTY_ANSWER
        for piece in _chunks(answer_text):
            yield {"type": "message.delta", "data": {"text": piece}}


__all__ = [
    "CoreRagAnswerProvider",
    "RagGenerate",
    "ResolveOwner",
]
