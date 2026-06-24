# Refactoring Review

作成日: 2026-06-18

このディレクトリは、コードベース全体のリファクタリング候補と改善案を集約する置き場です。
本メモは 2026-06-18 時点の静的調査、既存テスト実行、lint/build 結果に基づくレビューです。

## 調査範囲

- Backend: `packages/api/jetuse_core`, `packages/api/service`, `packages/api/fn`
- Frontend: `packages/web/src`
- Agent containers: `packages/agent-containers`
- Infra/Ops: `infra/terraform`, `ops`
- Tests/CI: `packages/api/tests`, `.github/workflows/ci.yml`

生成物・依存物はレビュー対象から除外しました。

- `packages/web/node_modules`
- `packages/web/dist`
- `infra/terraform/environments/*/.terraform`
- `*.tfstate`, `*.tfvars`
- `__pycache__`, `.ruff_cache`

## 検証結果

| コマンド | 結果 |
|---|---|
| `.venv/bin/python -m ruff check packages/api packages/agent-containers ops` | pass |
| `cd packages/web && npm run lint` | pass |
| `cd packages/web && npm run build` | pass, ただし 805KB の main chunk 警告あり |
| `.venv/bin/python -m pytest packages/api/tests` | 118 passed / 7 failed |

pytest の失敗は agent framework/model/tool validation の移行境界に集中しています。
初回 sandbox 内実行では localhost 接続テストも 1 件失敗しましたが、権限付き再実行では通りました。

## 優先度付き改善案

| 優先度 | 項目 | 対象 | 改善案 |
|---|---|---|---|
| P0 | API テスト失敗 | `packages/api/tests/test_agent.py`, `packages/api/tests/test_agents_sdk.py`, `packages/api/tests/test_hosted_agent.py`, `packages/api/service/main.py` | ADR-0009 の hosted container 方針を正として、旧 `agents_sdk` / `hosted` framework 値の扱いを決める。互換維持なら Pydantic 前段で正規化し、維持しないならテストを現行仕様へ更新する。 |
| P1 | `service/main.py` の肥大化 | `packages/api/service/main.py` | route module と service layer に分割する。特に `chat_stream` を validation / RAG dispatch / agent dispatch / normal stream / persistence-audit に切る。 |
| P1 | Frontend 巨大コンポーネント | `packages/web/src/pages/chat.tsx`, `dbchat.tsx`, `voicechat.tsx` | `useChatStream`, `useConversations`, `ToolPanel`, `MessageList`, `Composer` などに分割する。最初は `chat.tsx` を対象にする。 |
| P1 | SSE parse の重複 | `packages/web/src/pages/*.tsx` | `src/lib/sse.ts` を作り、`readSse()` で `TextDecoder`、`\n\n` 分割、`data:` parse、401/Abort handling を共通化する。 |
| P2 | hosted 移行後の dead code | `packages/api/jetuse_core/agents_sdk.py`, `packages/api/jetuse_core/langgraph_engine.py` | 現行 service から呼ばれていない。削除するか `legacy/` に隔離し、docs/tests を hosted container 前提へ寄せる。 |
| P2 | ツール実装の二重管理 | `jetuse_core/tools.py`, `jetuse_core/webtools.py`, `packages/agent-containers/agent_common.py`, `agent_db.py` | API と agent container で共有 wheel を使う。コンテナ側は adapter と env/config 差分だけにする。 |
| P2 | テスト不足 | `packages/web`, CI | Frontend unit/spec がない。Vitest で `ucform`, SSE helper, auth/header, chat reducer 相当を追加し、Playwright smoke を CI または手動検証に組み込む。 |
| P3 | 未使用・残骸候補 | `test.txt`, `vite.svg`, `gallery.Nav`, `datasets.get_owner_tables`, `rag_opensearch.delete_owner` | 意図がなければ削除。残す場合は呼び出し経路かテストを追加する。 |

## 重複コード

### SSE stream parsing

同じ `fetch -> reader.getReader() -> TextDecoder -> "\n\n" split -> data line parse`
の処理が複数画面にあります。

確認できた主な重複:

- `packages/web/src/pages/chat.tsx`
- `packages/web/src/pages/dbchat.tsx`
- `packages/web/src/pages/rag.tsx`
- `packages/web/src/pages/usecase.tsx`
- `packages/web/src/pages/minutes.tsx`
- `packages/web/src/pages/voicechat.tsx`
- `packages/web/src/pages/realtime.tsx`
- `packages/web/src/pages/video.tsx`

改善案:

- `packages/web/src/lib/sse.ts` を追加する。
- API ごとの payload は callback で処理する。
- 401 時の `reauthenticate()`、AbortError の無視、JSON parse error の表示方針を統一する。

### Tool / web extraction logic

API 側と agent container 側で、Web 検索・Web 取得・SSRF ガードの近い実装が複製されています。

- `packages/api/jetuse_core/tools.py`
- `packages/api/jetuse_core/webtools.py`
- `packages/agent-containers/agent_common.py`

`agent_common.py` には「jetuse_core/webtools から移植」「jetuse_core/tools から移植」という形の処理があり、
SSRF 対策や HTML parser の修正が二重管理になります。

改善案:

- `jetuse_core` を agent container image へ同梱し、同じ関数を import する。
- container 固有の制約は wrapper に閉じ込める。
- `web_search`, `web_fetch`, `get_current_time`, `rag_search`, `query_database` の contract を共通テスト化する。

### NL2SQL execution

`packages/api/jetuse_core/nl2sql.py` と `packages/agent-containers/agent_db.py` に、
SQL Search 呼び出し、readonly 実行、SQL sanitization の類似処理があります。

改善案:

- DB 接続設定だけを adapter 化し、SQL 生成・sanitize・result shaping は共通化する。
- container 側の env 依存は `AgentDbConfig` のような薄い設定 object に閉じ込める。

## 巨大ファイル・巨大関数

### Backend

| 対象 | 規模 | 問題 |
|---|---:|---|
| `packages/api/service/main.py` | 約1550行 | FastAPI app、DTO、全 route、SSE、agent/RAG/NL2SQL/minutes/STT などが同居 |
| `create_app()` | 約1291行 | route 登録と個別業務ロジックが単一関数に集約 |
| `chat_stream()` | 約474行 | validation、RAG、agent、画像、会話永続化、監査、SSE が混在 |
| `jetuse_core/chat.py::stream_agent()` | 約216行 | Responses API tool loop と承認/自動実行が集中 |
| `jetuse_core/agents_sdk.py::stream_agents_sdk()` | 約131行 | 現行 service からは未使用の旧 in-process 経路 |

推奨分割:

- `service/routes/chat.py`
- `service/routes/agents.py`
- `service/routes/rag.py`
- `service/routes/dbchat.py`
- `service/sse.py`
- `service/agent_dispatch.py`
- `service/schemas.py`

### Frontend

| 対象 | 規模 | 問題 |
|---|---:|---|
| `packages/web/src/pages/chat.tsx` | 約1189行、`Chat()` 約1154行 | 状態、API、SSE、ツール承認、履歴、UI が集中 |
| `packages/web/src/prefs.tsx` | 約703行 | i18n 辞書と provider が同居 |
| `packages/web/src/pages/dbchat.tsx` | 約645行、`DbChat()` 約622行 | dataset/NL2SQL/chart/table UI が集中 |
| `packages/web/src/pages/voicechat.tsx` | 約452行、`VoiceChat()` 約403行 | 録音、STT、TTS、SSE、UI が集中 |

推奨分割:

- `src/lib/sse.ts`
- `src/lib/api.ts`
- `src/pages/chat/useChatStream.ts`
- `src/pages/chat/useConversations.ts`
- `src/pages/chat/MessageList.tsx`
- `src/pages/chat/Composer.tsx`
- `src/pages/chat/ToolPanel.tsx`
- `src/i18n/dict.ja.ts`, `src/i18n/dict.en.ts`

## 巨大クラス

問題になる巨大クラスはほぼありません。

最大級は以下ですが、いずれも DTO / settings 的な役割です。

- `packages/api/jetuse_core/settings.py::Settings`
- `packages/api/service/main.py::AgentDefinition`

ただし `AgentDefinition.validated()` は agent framework 移行の仕様差分を抱えており、
route schema から service-level validator へ移す方がテストしやすくなります。

## 未使用コード・残骸候補

静的参照検索で、以下は定義以外の参照が見当たりませんでした。

| 候補 | 備考 |
|---|---|
| `packages/api/jetuse_core/agents_sdk.py` | docs 上でも hosted 移行後の dead code と記載あり |
| `packages/api/jetuse_core/langgraph_engine.py` | docs 上でも hosted 移行後の dead code と記載あり |
| `packages/api/jetuse_core/datasets.py::get_owner_tables()` | 呼び出しなし |
| `packages/api/jetuse_core/rag_opensearch.py::delete_owner()` | 呼び出しなし |
| `packages/web/src/components/gallery.tsx::Nav` | export されているが import なし |
| `packages/web/src/assets/vite.svg` | 参照なし |
| `test.txt` | 内容は `aatest:` のみ。参照なし |

注意:

- FastAPI route 関数は decorator 経由で使われるため、単純な参照数では未使用判定できません。
- export された UI 部品はデザインギャラリー用途の可能性があります。削除前に意図確認が必要です。

## 循環依存

Python と TypeScript の import graph を簡易 Tarjan で確認しました。

- Python: 循環依存なし
- TypeScript: 循環依存なし

現時点では循環依存は優先課題ではありません。

## テスト不足

### Backend

API テストは 125 件ありますが、現在 7 件が失敗しています。

失敗の集中箇所:

- agent framework value の互換性
- hosted container routing と旧 in-process `agents_sdk` 期待の混在
- tool validation の仕様差分
- `langgraph` / `hosted` の旧テスト期待

改善案:

- ADR-0009 hosted routing を正とした agent validation test に更新する。
- 旧値 `native`, `agents_sdk`, `hosted` を許容する場合は `normalize_framework()` の単体テストを作る。
- hosted container 未設定時の 503 と、定義 validation の 422 を分離してテストする。
- coverage 閾値を小さくてもよいので導入し、重要領域だけ下限を設定する。

### Frontend

`packages/web/src` 配下に unit/spec test はありません。
CI は `npm run lint` と `npm run build` のみです。

優先して追加するテスト:

1. `ucform` の template rendering / required validation
2. 共通化後の SSE parser
3. auth header / 401 reauth handling
4. chat send payload construction
5. tool approval state transition

### Infra/Ops

CI は `terraform fmt -check -recursive` と `terraform validate` まであります。
一方で `ops/*.sh` / `ops/*.py` の smoke test や shellcheck はありません。

改善案:

- `shellcheck ops/*.sh`
- Terraform module 単位の `terraform validate`
- `ops` Python script の import/parse smoke

## 推奨ロードマップ

### Phase 1: CI green 化

- agent framework 仕様を ADR-0009 に合わせる。
- 旧 framework 値を許容するか削除するか決める。
- pytest 7 failure を解消する。

### Phase 2: 重複削減

- Frontend SSE parser を共通化する。
- API/container の tool/web extraction 実装を共通化する。
- `agents_sdk.py` / `langgraph_engine.py` の扱いを決める。

### Phase 3: 巨大関数分割

- `service/main.py` から schemas/routes/helpers を分離する。
- `chat_stream()` を agent/RAG/normal stream の dispatch に切る。
- `chat.tsx` を hooks と UI components に切る。

### Phase 4: テスト強化

- Frontend に Vitest を導入する。
- SSE helper と chat payload construction をテストする。
- Playwright smoke を主要画面に追加する。
- coverage の最低ラインを CI に入れる。

## 補足: 現時点の作業ツリー

レビュー中に確認した時点では、作業ツリーに既存の未コミット変更がありました。
このメモ作成では既存変更を戻していません。
