# Review Validation Audit

作成日: 2026-06-18

対象: [`docs/refactoring/review-validation.md`](./review-validation.md)

このファイルは、`review-validation.md` の妥当性を、再度コードベースに突合して確認した監査メモです。
`review-validation.md` は大筋で妥当ですが、実装着手前に補正した方がよい点があります。

## 結論

`review-validation.md` は正本候補として使えます。
ただし、以下の5点は補正推奨です。

1. `stream_agent()` は「現行の唯一のagent実行経路」ではない。
2. `agents_sdk.py` / `langgraph_engine.py` の削除は、production service 未使用ではあるが「極低リスク」とまでは言えない。
3. 旧 framework 値を「実在しない」と判断する前に、既存DBデータと互換マッピングの扱いを決める必要がある。
4. `test_langgraph_requires_auto_tools` は、DB依存の順序問題だけでなく、旧仕様の期待自体を見直すべき。
5. bundle 分割の追加提案は妥当だが、`mermaid` / `chart.js` は既に dynamic import 済み。

## 再検証コマンド

| コマンド | 結果 |
|---|---|
| `.venv/bin/ruff check packages/api packages/agent-containers ops` | pass |
| `cd packages/web && npm run lint` | pass |
| `cd packages/web && npm run build` | pass, main chunk 805KB 警告あり |
| `.venv/bin/python -m pytest packages/api/tests -q` | 118 passed / 7 failed |

`pytest` は localhost を使う切断テストを含むため、sandbox の接続制限を避けて権限付きで再実行しました。

## 妥当だった点

### P0: API テスト失敗の分析

妥当です。失敗は agent framework 移行境界に集中しています。

- `packages/api/tests/test_agent.py`
- `packages/api/tests/test_agents_sdk.py`
- `packages/api/tests/test_hosted_agent.py`

現行 schema は `framework: Literal["openai_agents", "adk", "langgraph", "select_ai"]` です。
旧値 `agents_sdk` / `hosted` を送るテストは Pydantic の Literal validation で落ちます。

関連箇所:

- `packages/api/service/main.py:139-188`
- `packages/api/tests/test_agents_sdk.py:9`
- `packages/api/tests/test_hosted_agent.py:22-35`

### SSE parser 重複

妥当です。`getReader()` / `TextDecoder` / `\n\n` split の処理は8ファイルにあり、
`voicechat.tsx` には2箇所あります。

確認箇所:

- `packages/web/src/pages/chat.tsx:380`
- `packages/web/src/pages/dbchat.tsx:198`
- `packages/web/src/pages/rag.tsx:138`
- `packages/web/src/pages/usecase.tsx:99`
- `packages/web/src/pages/minutes.tsx:143`
- `packages/web/src/pages/voicechat.tsx:117`
- `packages/web/src/pages/voicechat.tsx:316`
- `packages/web/src/pages/realtime.tsx:108`
- `packages/web/src/pages/video.tsx:114`

`packages/web/src/lib/sse.ts` への共通化と、その helper の Vitest 追加は妥当な優先施策です。

### Tool / Web extraction の重複

妥当です。`agent_common.py` には API 側から移植した旨のコメントがあり、
DDG parser、SSRF guard、web fetch、current time が二重管理です。

確認箇所:

- `packages/api/jetuse_core/tools.py`
- `packages/api/jetuse_core/webtools.py`
- `packages/agent-containers/agent_common.py`

また NL2SQL も以下で近い実装があります。

- `packages/api/jetuse_core/nl2sql.py`
- `packages/agent-containers/agent_db.py`

`Containerfile.*` は `agent_common.py agent_db.py server.py run_*.py` のみを COPY しており、
`jetuse_core` をそのまま import する構成ではありません。
したがって `jetuse_shared` のような最小共有パッケージを切る補正は妥当です。

### 巨大ファイル・巨大関数

概ね妥当です。

再計測結果:

- `packages/api/service/main.py`: 1550行
- `create_app()`: 1291行
- `chat_stream()`: 474行
- `jetuse_core/chat.py::stream_agent()`: 216行
- `packages/web/src/pages/chat.tsx`: 1194行
- `Chat()`: 1154行
- `packages/web/src/pages/dbchat.tsx`: 645行
- `DbChat()`: 622行
- `packages/web/src/pages/voicechat.tsx`: 452行
- `VoiceChat()`: 403行

## 補正推奨点

### 1. `stream_agent()` は唯一の agent 実行経路ではない

`review-validation.md` では `jetuse_core/chat.py::stream_agent()` を
「現行の唯一のagent実行経路」としていますが、これは不正確です。

保存済み agent は、`/api/chat/stream` 内で早期 return されます。

- `framework == "select_ai"` は `select_ai_agent.run()` へ dispatch
- それ以外の保存済み agent は `hosted_agent.invoke_agent()` へ dispatch
- `stream_agent()` は主に `req.agent` 直指定の旧/native Responses ReAct モード用

関連箇所:

- `packages/api/service/main.py:441-482`
- `packages/api/service/main.py:487-555`
- `packages/api/service/main.py:655-677`

推奨表現:

> `stream_agent()` は現行でも `req.agent` 直指定モードで使われるが、保存済み agent の主経路は hosted container / Select AI へ移行済み。

### 2. dead code 削除はテスト更新とセットにする

`agents_sdk.py` と `langgraph_engine.py` は production service からは未呼び出しです。
この点は妥当です。

ただし、以下の参照が残っています。

- `packages/api/tests/test_agents_sdk.py` が `jetuse_core.agents_sdk` を import
- `packages/api/jetuse_core/langgraph_engine.py` が `.agents_sdk` を import
- `service/main.py` / `chat.tsx` に `sdk_state` / `sdk_approvals` の残骸がある
- docs/specs に旧 in-process engine の記述が複数残る

そのため、削除は「production service 未使用なので候補として妥当」ですが、
「極低リスク」とは言い切れません。

推奨順序:

1. agent framework テストを ADR-0009 hosted routing 前提に更新
2. `sdk_state` / `sdk_approvals` の要否を確認して削除または legacy 化
3. `agents_sdk.py` / `langgraph_engine.py` を削除
4. specs/docs の旧記述を ADR-0009 へ寄せる

### 3. 旧 framework 値の扱いは既存データ確認が必要

`review-validation.md` は、旧値 `agents_sdk` / `hosted` / `native` について
「後方互換が必要なケースが実在しないことを確認の上、旧値は受理しない方針を推奨」
としています。

方針としては妥当ですが、現行コードには互換を意識した実装が残っています。

- `packages/api/jetuse_core/hosted_agent.py:91-100`
  - `agents_sdk`, `native`, `hosted` を `openai_agents` に normalize
- `packages/web/src/pages/agentbuilder.tsx:55-63`
  - 旧 framework 値を UI 上 `openai_agents` へ読み替え
- `packages/web/src/pages/agents.tsx`
  - 旧 framework 値の表示 mapping が残る
- `packages/api/jetuse_core/migrations/010_agent_framework.sql`
  - `framework VARCHAR2(20) DEFAULT 'native' NOT NULL`

つまり、旧値を持つ既存DBレコードが存在しうる設計痕跡があります。
旧値拒否に進む前に、DB migration または read-time normalize の方針を決める必要があります。

推奨:

- 既存 agents table の `framework` 分布を確認する。
- `native` / `agents_sdk` / `hosted` が残るなら migration で `openai_agents` に更新する。
- API input と DB read のどちらで normalize するかを明文化する。

### 4. `test_langgraph_requires_auto_tools` は旧仕様の見直しが必要

`review-validation.md` は、このテストについて
「503（DB未接続）で落ちているため、422検証がDB接続前に走るよう順序を直す」
と書いています。

ただし、現行 ADR-0009 hosted routing では、LangGraph は hosted container 側で実行されます。
旧 in-process `langgraph_engine.py` の「承認フローなしなので auto_tools 必須」という制約を、
現行 API 定義 validation に残すべきかは再検討が必要です。

現在の `AgentDefinition.validated()` には `langgraph + tools + auto_tools=false` を
拒否するロジックはありません。

推奨:

- 旧仕様を維持して 422 を復活させるのではなく、ADR-0009 の現行仕様に合わせてテスト期待を更新する。
- hosted container で `auto_tools` をどう扱うかを仕様化する。

### 5. Bundle 分割の具体例は補正する

`review-validation.md` の追加提案で、805KB main chunk 対策として
`mermaid / katex / chart` の dynamic import が挙げられています。

方向性は妥当ですが、少なくとも `mermaid` と `chart.js` は既に dynamic import 済みです。

確認箇所:

- `packages/web/src/components/markdown.tsx:66`
- `packages/web/src/components/resultchart.tsx:39`

次に見るべき候補:

- route/component 単位の `React.lazy`
- `react-markdown`, `rehype-highlight`, `remark-gfm`, `lowlight` の遅延ロード
- Markdown renderer をチャット画面内で必要時だけ読み込む
- design/gallery 系や admin/dbchart 系の route chunk 分割

## 循環依存

今回も Python / TypeScript ともに import cycle は検出されませんでした。

- Python: cycle なし
- TypeScript: cycle なし

## 最終判断

`review-validation.md` は、以下の補正を入れれば実装計画として十分に信頼できます。

- `stream_agent()` の位置付けを修正する。
- dead code 削除を「テスト・docs 更新とセット」にする。
- 旧 framework 値の既存データ移行方針を追加する。
- `langgraph auto_tools` テストは旧仕様維持ではなく、ADR-0009 準拠で期待値を再定義する。
- bundle 分割の具体候補を、既に dynamic import 済みのものから route/Markdown 系へ更新する。
