# specs/14: Phase 9 エージェント開発フレームワーク対応（FW-01〜04）

> **改訂（2026-06-18・ADR-0009準拠／refactoring P0.7）**: 本仕様の **インプロセスエンジン**
> （`jetuse_core/agents_sdk.py` の `stream_agents_sdk` = FW-01、`jetuse_core/langgraph_engine.py` の
> `stream_langgraph` = FW-02）は **ADR-0009 で hosted コンテナ（3SDK別 Hosted Application）へ
> 置換済みのため削除した**。以下の FW-01/FW-02 のインプロセス実装記述は**歴史的経緯**であり、
> 現行のエージェント実行経路は ADR-0009（`hosted_agent.invoke_agent`）と
> `jetuse_core/chat.py::stream_agent`（アドホック native Responses）を参照のこと。
> framework 値そのもの（`langgraph` 等）と read-time 正規化（`normalize_sdk`/ADR-0010）は現役。

OCI Enterprise AIのOpen Responses Spec互換を利用し、主要エージェントFWの実装例と
フルスクラッチ実装（AGT-01）との比較材料を整備する（docs/plan.md §11）。

## [FW-01] OpenAI Agents SDK

### 実機確定事項（SPIKE-12、spikes/spike12_agents_sdk.py。原因分析の正本はADR-0007）

| 項目 | 結果 |
|---|---|
| SDK既定（Responsesモデル） | **不可** — SDKは `type:"message"` 無しの簡易inputを送り、OCIの厳格スキーマが拒否（`Invalid 'input'`）。さらに `OpenAi-Project` ヘッダ無しだと「Compartment ID must be provided」という誤誘導エラー |
| **OpenAIChatCompletionsModel** | **全機能OK**: 基本実行 / function tool / handoffs / guardrails / streaming（gpt-ossはchat completionsエンドポイントでも動く — 新発見） |
| tracing | OpenAI本家エンドポイントへ送ろうとするため `set_tracing_disabled(True)` 必須 |
| function calling引数 | chat completions経由のgpt-ossは空キー等の崩れた引数を出すことがある → スキーマ外キーを除去する寛容化が必要 |

### 実装
- `jetuse_core/agents_sdk.py`: 既存ツールレジストリ(AGT-01)を `FunctionTool` にラップ
  （`strict_json_schema=False`、自動実行のみ）。`Runner.run_streamed` のイベントを
  本アプリのSSE形（delta / tool_call通知 / usage）へ正規化。handoffは `Handoff → X` 通知
- AGENTSテーブルに `framework`（'native' | 'agents_sdk'、migration 010）
- `/api/chat/stream`: agents_sdkエージェントはasyncネイティブ分岐（会話永続化は共通対応）
- Agent Builder UI: 実装（エンジン）選択を追加

### v1制約
- ツールは**自動実行のみ**（承認フローはnativeパスの機能）
- MCP / rag_search / code_interpreter（Responses built-in）非対応 — 422でガード
- 短期メモリ（OCI Conversations）非統合（ステートレス、履歴全送信）

### 完了条件
- ローカル+実環境で、SDKエージェントの多段ツール実行がチャットUIから動作
- 検証レポート docs/verification/FW-01.md

## [FW-02] LangGraph

### 方式決定
**インプロセスエンジン**として統合する（FW-01と同じ`framework`選択軸に載せ、比較可能性を確保）。
ホスト型（Applications/Deployments）での稼働はAGT-04で実証済みのため重複検証しない。

### 実機確定事項（SPIKE-13、spikes/spike13_langgraph.py）
- `ChatOpenAI`(base_url=OCI互換 + IAM署名httpx) で 基本/ツール/astream_events/並列分岐グラフ 全部OK
- `create_react_agent` はlanggraph v1で非推奨（v2削除予定）→ 依存を `langgraph>=1,<2` にpin
- `StructuredTool` は dict JSONスキーマの `args_schema` を受ける（pydanticモデル生成不要）

### 実装
- `jetuse_core/langgraph_engine.py`: prebuilt ReActグラフ + 既存ツールラップ。
  `astream_events` を本アプリSSE形へ正規化。**再帰上限到達時はon_tool_endで収集した
  ツール結果を使いクローズ回答**（FW-01cと同等の挙動）
- `framework='langgraph'`（migration 010の列を共用）。Builderにエンジン選択追加

### v1制約（422でガード）
- ツールは自動実行のみ（auto_tools必須 — LangGraphの承認はcheckpointer前提でステートレス往復に
  載せにくい。FW-04の比較材料）
- MCP / code_interpreter 非対応

## [FW-03] その他FW互換性検証 / [FW-04] 比較整理
- CrewAI / AI SDK / LangChain の最小互換検証 → `docs/comparison/agent-frameworks.md`
- フルスクラッチ(native) vs SDK vs LangGraph の使い分け（顧客類型別の推奨）
