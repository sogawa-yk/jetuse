# Refactoring Review — 検証済み版 (validated)

作成日: 2026-06-18
最終更新: 2026-06-18（監査 [review-validation-audit.md](./review-validation-audit.md) の指摘5点を反映）
元レビュー: [README.md](./README.md)（Codexによる初版・2026-06-18）

> 本ファイルは README.md の内容を**実装と突合して検証し、補正・優先順位調整を加えた単独完結版**です。
> 「何を・どこで・なぜ・どの順でリファクタリングするか」がこのファイルだけで分かるよう、
> 元レビューと重複する内容も再掲しています。実装着手時はこのファイルを正とします。
>
> **改訂履歴**: 本版は監査メモ [review-validation-audit.md](./review-validation-audit.md) の5点（コードで全件確認済み）を反映済み:
> ① `stream_agent()` は唯一の経路ではない（§5）/ ② デッドコード削除はテスト・docs更新とセット＝リスク「低」（§2）/
> ③ 旧framework値のDBレコードが実在しうる＝移行方針が必要（§1, 新設§1.5）/ ④ langgraph auto_tools テストは旧仕様復活でなくADR-0009準拠で再定義（§1）/
> ⑤ mermaid/chart.js は既にdynamic import済み＝候補をroute/Markdown系へ（§8）。

---

## 0. 検証方法と結論

- 実コードへの突合（`wc -l`・import グラフ・該当行の引用）、`pytest`/`ruff`/`npm` 実行、コンテナの `Containerfile` 確認で各主張を検証。
- **総評**: 事実認定はほぼ全て正確。改善方針も妥当。ただし (a) エージェントの framework 用語、(b) コンテナ間共通化の実現性、で重要な補正が必要。優先順位も一部入替。
- 判定凡例: ✅検証OK / ⚠️要補正 / ❌誤り。

### 検証コマンド結果（2026-06-18 時点）

| コマンド | 結果 |
|---|---|
| `.venv/bin/ruff check packages/api packages/agent-containers ops` | pass |
| `cd packages/web && npm run lint` | pass |
| `cd packages/web && npm run build` | pass（main chunk **805KB** 警告あり） |
| `cd packages/api && pytest -q` | **118 passed / 7 failed** |

`.github/workflows/ci.yml` は `ruff check .`(L22) と `pytest -q`(L24) を実行する。
→ **7失敗により main の CI は現在 RED**（直接マージ運用のためマージはブロックされていないが、赤のまま）。

---

## 1. P0 — CIを緑にする（最優先・低リスク）

### 事実（✅検証OK・一部⚠️補正）

- 失敗は `test_agent.py` / `test_agents_sdk.py` / `test_hosted_agent.py` の7件。
- 現行の framework 定義（`service/main.py` 内 `AgentDefinition`）:
  - `framework: Literal["openai_agents", "adk", "langgraph", "select_ai"]`、既定 `"openai_agents"`
  - `AgentDefinition.validated()` は `service/main.py:155-188`。`select_ai` は `select_ai_agent.VALID_TOOLS`、それ以外（hostedコンテナ）は `{web_search, web_fetch, get_current_time, query_database, rag_search}` のみ許可。
- 失敗の主因は **テストが旧 framework 値 `"agents_sdk"` / `"hosted"` / `"native"` を使用**しており、Pydantic が Literal 不一致で弾く（ADR-0009のhostedルーティングへ移行済みのため）。
  - 例: `test_agents_sdk.py` BASE は `framework="agents_sdk"`（現行は `openai_agents`）。
  - 例: `test_agent.py:391` は全ツール受理を期待するが `code_interpreter` 非対応で 422。
  - 例: `test_agent.py:296` は `llama-3.3-70b`+tools に 422 を期待するが実際 200。

### ⚠️補正（README・本doc初版からの修正点）

- **「バグか古いテストか」→ ほぼ"古いテスト"**。hostedルーティングではツールはコンテナ内でそのSDKのモデルが実行するため、「ツール付きはResponses系モデルのみ」という旧制約は無効。`test_agent.py:296` の期待は**仕様変更による陳腐化**であり main 側のバグではない。
- `test_agents_sdk.py::test_langgraph_requires_auto_tools`（旧FW-02仕様「langgraphは承認フローなし→ツールありは auto_tools 必須」で 422 を期待）について、**現行 `validated()`(main.py:155-188) に langgraph+auto_tools の拒否ルールは存在しない**（検証は model / select_ai tools / hostedコンテナtools / MCP のみ）。そのため検証を通過し、agent作成のDB書込で 503 になる。
  - → **単なるDB順序問題ではない**。旧仕様の 422 を復活させるのではなく、**ADR-0009準拠で期待値を再定義**する（hosted container では auto_tools をどう扱うかを先に仕様化）。本doc初版の「422検証をDB前に走らせる」だけでは不十分。
- 旧 framework 値は **入力(API)では現行 Literal が拒否**するが、**DB側には旧値が残存しうる**（後方互換 normalize が実在。詳細は §1.5）。本doc初版の「旧値は実在しない」は誤り。

### やること

1. テストを **ADR-0009（hostedルーティング）準拠**へ更新する。
   - テスト内の旧値 `agents_sdk`/`hosted`/`native` を現行値 `openai_agents`/`adk`/`langgraph`/`select_ai` に置換。
   - `test_langgraph_requires_auto_tools` は 422期待を撤回し、ADR-0009のhosted仕様に合わせた期待へ書き換える（§1.5の方針と整合）。
   - 入力(API)の旧値拒否は現行 Literal のままでよい。**DB残存値の扱いは §1.5 で別途決める**（read-time normalize は既に実在）。
2. hostedコンテナ未設定時の **503** と、定義 validation の **422** を分離してテストする（DB依存を除去）。
3. 軽量で良いので coverage 閾値導入（重要領域だけ下限）。

対象: `packages/api/tests/test_agent.py`, `test_agents_sdk.py`, `test_hosted_agent.py`, （必要なら）`packages/api/service/main.py` の `AgentDefinition.validated()`。

---

## 1.5 P0.6 — 旧 framework 値の既存DBデータ移行方針（⚠️監査で新設）

### 事実（✅検証OK）

入力スキーマは現行 Literal のみ受理するが、**旧値を持つDBレコードが実在しうる**設計痕跡がある:

- `packages/api/jetuse_core/hosted_agent.py` に後方互換マッピング `_LEGACY_SDK = {"agents_sdk":"openai_agents", "native":"openai_agents", "hosted":"openai_agents", ...}` と `normalize_sdk()` が実在し、**読取時に使用**（`service/main.py:488` の `hosted_agent.normalize_sdk(agent_def.get("framework"))`）。
- `packages/api/jetuse_core/migrations/010_agent_framework.sql:1` は `ALTER TABLE agents ADD framework VARCHAR2(20) DEFAULT 'native' NOT NULL` → 既存行は **`'native'`** を持つ。
- フロントにも読み替えが残る: `packages/web/src/pages/agentbuilder.tsx:55-63`、`packages/web/src/pages/agents.tsx`（旧値→`openai_agents` 表示）。

### やること（旧値拒否や normalize 整理の前に）

1. 既存分布を確認: `SELECT framework, COUNT(*) FROM agents GROUP BY framework`。
2. `native`/`agents_sdk`/`hosted` が残るなら **migration で `openai_agents` 等へ UPDATE**（新規 `011+` マイグレーション）。
3. **read-time `normalize_sdk` を正**としつつ、移行後は冗長になる箇所（フロントの読み替え等）を整理。normalize をどこで一元化するか（API read か DB か）を明文化。
4. これらが済むまで `normalize_sdk` / `_LEGACY_SDK` は**残す**（消すと旧DB行が壊れる）。

> 順序上の含意: §2 のデッドコード削除（agents_sdk/langgraph_engine）とは別物。`normalize_sdk` は hosted ルーティングの現役コードであり削除対象ではない。

---

## 2. P0.5 — デッドコード削除

静的参照検索で定義以外の参照が無いことを確認済み（✅検証OK）。
**リスクは2群に分かれる**ので分けて扱う（監査補正）。

### 2-A. 即削除可（リスク極低・他依存なし）

| 対象 | 場所 | 検証 |
|---|---|---|
| `get_owner_tables()` | `packages/api/jetuse_core/datasets.py:236` | 呼び出し0 |
| `delete_owner()` | `packages/api/jetuse_core/rag_opensearch.py:133` | 呼び出し0 |
| `Nav`（export済み未使用） | `packages/web/src/components/gallery.tsx:49` | どこからも import されていない（同ファイル内の他export は design.tsx 等で使用中） |
| `vite.svg` | `packages/web/src/assets/vite.svg` | `src/` 内に参照なし |
| `test.txt` | リポジトリ直下 | 中身は `aatest:` のみ。参照なし（リモートの "test" コミット由来） |

### 2-B. 削除は「テスト・docs更新とセット」（リスク低・⚠️監査補正）

| 対象 | 場所 | 検証 / 残存依存 |
|---|---|---|
| `agents_sdk.py`（`stream_agents_sdk`） | `packages/api/jetuse_core/agents_sdk.py` | production service 未呼び出し。だが **`tests/test_agents_sdk.py:46` が `from jetuse_core import agents_sdk`**、**`langgraph_engine.py:20` が `from .agents_sdk import ...`** で依存 |
| `langgraph_engine.py`（`stream_langgraph`） | `packages/api/jetuse_core/langgraph_engine.py` | service 参照0。`agents_sdk.py` を import しており**2つで1つのデッド・クラスタ** |

これらは「production未使用だから候補として妥当」だが、テスト・相互依存・`sdk_state`/`sdk_approvals` 残骸・docs/specs記述が絡むため**「極低」ではなく「低」**。下記の順で外す。

推奨順序:
1. agent framework テストを ADR-0009 hosted routing 前提へ更新（§1）。`tests/test_agents_sdk.py` の `agents_sdk` import 依存も解消。
2. `service/main.py` / `packages/web/src/pages/chat.tsx` の `sdk_state` / `sdk_approvals`（FW-01b の承認往復UI残骸）の要否を確認し、削除または legacy 化。
3. `agents_sdk.py` / `langgraph_engine.py` を**まとめて**削除。
4. `specs/` / `docs/` の旧 in-process engine 記述を ADR-0009 へ寄せ、ADR-0009 に「インプロセス3エンジンは hosted コンテナへ置換済み」と追記。

### ⚠️注意

- `agents_sdk.py` / `langgraph_engine.py` は **FW-01/FW-02 の検証成果**（`docs/verification/FW-01.md`, `FW-02.md`）。git履歴に残るため削除可。
- **`langgraph` という framework 値は現役**（hosted langgraph コンテナへルーティング）。消すのは `langgraph_engine.py`（インプロセス実装）であって framework 値ではない。同様に **`hosted_agent.normalize_sdk`/`_LEGACY_SDK` は現役**（§1.5）で削除対象外。
- FastAPI route 関数は decorator 経由で使われるため、単純な参照数では未使用判定しない。`Nav` は design.tsx でも未使用と確認済み。

---

## 3. P1 — フロントエンド SSE パーサの共通化（高ROI・低リスク）

### 事実（✅検証OK）

同一の `fetch → res.body.getReader() → new TextDecoder() → "\n\n" split → "data:" parse` ループが**8ファイル**に重複。

| ファイル | 該当行 |
|---|---|
| `packages/web/src/pages/chat.tsx` | 380-391 |
| `packages/web/src/pages/dbchat.tsx` | 198-209 |
| `packages/web/src/pages/rag.tsx` | 138-156 |
| `packages/web/src/pages/usecase.tsx` | 99-110 |
| `packages/web/src/pages/minutes.tsx` | 143-154 |
| `packages/web/src/pages/voicechat.tsx` | 117-128（および 316-327 の2箇所） |
| `packages/web/src/pages/realtime.tsx` | 108-119 |
| `packages/web/src/pages/video.tsx` | 114-125 |

共通の形（chat.tsx:380 付近）:
```ts
const reader = res.body.getReader()
const dec = new TextDecoder()
let buf = ''
for (;;) {
  const { done, value } = await reader.read()
  if (done) break
  buf += dec.decode(value, { stream: true })
  const parts = buf.split('\n\n')
  buf = parts.pop() ?? ''
  for (const part of parts) {
    const line = part.trim()
    if (!line.startsWith('data:')) continue
    const data = line.slice(5).trim()
    if (data === '[DONE]') continue
    const ev = JSON.parse(data) as { /* 画面ごと */ }
    ...
  }
}
```

吸収すべき差分（共通化で対応必須）:
- **401処理が2系統**: `throw new Error(...)` 系（chat/dbchat/rag/usecase/minutes/video）と `return reauthenticate()` 系（voicechat/realtime/minutes一部）。
- **Abort処理が2系統**: `e.name==='AbortError'` を break する系と、`catch {}` で無視する系（voicechat）。
- **イベント型が画面ごとに異なる**（chat=`delta/tool_call/tool_result/sdk_approvals`, dbchat=`sql/error`, voicechat=`text/is_final/closed`, rag=`delta/citations` 等）。

### やること

- `packages/web/src/lib/sse.ts` を新設し `readSse<T>(res, onEvent, opts)` を提供。
  - `TextDecoder`・`\n\n`分割・`data:`抽出・`[DONE]`・JSON parse error 表示方針を集約。
  - 401挙動（throw/return）と Abort 無視を `opts` で選択可能に。
  - イベント型はジェネリクス、payload処理はコールバック。
- 8ファイルを順次置換（まず chat.tsx）。
- **このヘルパーの単体テスト**を Vitest で先に書く（P4の前倒し。分割の安全網になる）。

---

## 4. P1 — バックエンド/コンテナのツール・Web抽出の共通化（中工数・⚠️実現性に補正）

### 事実（✅重複は検証OK）

`packages/agent-containers/agent_common.py` に明示コメントあり:
- L80 `# ---- web_fetch(SSRF対策込み。jetuse_core/webtools から移植) ----`
- L153 `# ---- web_search(DuckDuckGo HTML。jetuse_core/tools から移植) ----`

ほぼ同一の実装:

| ロジック | API側 | コンテナ側 |
|---|---|---|
| DuckDuckGo HTML パーサ `_DdgParser` | `jetuse_core/tools.py:25-64` | `agent_common.py:154-189` |
| web_fetch 本体 | `jetuse_core/webtools.py:74-106` | `agent_common.py:125-150` |
| SSRFガード `_assert_public_host` | `jetuse_core/webtools.py:26-42` | `agent_common.py:84-93` |
| get_current_time | `jetuse_core/tools.py:87-97` | `agent_common.py:214-222` |

NL2SQL も重複:
- `_BANNED` 正規表現: `jetuse_core/nl2sql.py:159-163` ≒ `agent_db.py:104-106`
- SQLサニタイズ: `nl2sql.sanitize_sql()`(L166-177) ≒ `agent_db._sanitize()`(L109-118)（差は例外型 `SqlRejectedError` vs `ValueError` とdocstring有無）
- SemanticStore 生成: `nl2sql.py:43-68` ≒ `agent_db.py:79-101`

**実害**: SSRF対策やHTMLパーサ、SQLサニタイズの修正が二重管理。実際に定数が乖離している例 → `MAX_TEXT_CHARS` が API=20000 / コンテナ=8000（挙動差が既に発生）。

### ⚠️補正（README の「jetuse_core を import / wheel共有」は記載より重い）

- コンテナは `Containerfile.openai/langgraph/adk` が**ローカル `.py` のみ `COPY`** する独立ビルドコンテキストで、`jetuse_core` を含まない（API側だけ `COPY jetuse_core`）。`agent_common.py`/`agent_db.py` は `jetuse_core` を import していない。
- よって「同じ関数を import」は今すぐ不可。実現には：
  1. 新規共有パッケージ **`jetuse_shared`** を切り出す（**まずはセキュリティ要件＝SSRFガード・SQLサニタイズ・DDGパーサに限定**。全 jetuse_core 共有はコンテナの意図的な薄さ＝per-runtime requirements を壊すので非推奨）。
  2. `jetuse_core` を `jetuse_shared` 依存に変更。
  3. コンテナの requirements/wheel に `jetuse_shared` を載せ、`Containerfile.*` を COPY からpip取り込みへ。
- 設定モデル差（`jetuse_core.settings` pydantic vs コンテナの `os.environ`）と例外型差（`SsrfBlockedError`/`SqlRejectedError` vs `ValueError`）は薄い adapter で吸収する。

### やること

- `jetuse_shared`（最小）を作り、`web_fetch`/`web_search`/SSRFガード/`sanitize_sql` を一本化。
- `web_search`, `web_fetch`, `get_current_time`, `rag_search`, `query_database` の contract を共通テスト化。
- コンテナ固有の制約は wrapper / `AgentDbConfig` 的な薄い設定 object に閉じ込める。

### コンテナ構成（参考・✅確認済み）

`packages/agent-containers/`: `agent_common.py`(共有ツール実装) / `agent_db.py`(NL2SQL) / `server.py`(FastAPI雛形) / `run_openai.py` / `run_langgraph.py` / `run_adk.py` / `Containerfile.*` / `requirements-*.txt`。
ツール実装は3ランタイムで100%共有、各SDKへの「接続グルー」だけ `run_*.py` で差分。

---

## 5. P1 — 巨大ファイル/巨大関数の分割（⚠️テスト整備後に着手）

### 事実（✅線数は実測一致）

Backend:

| 対象 | 実測 | 問題 |
|---|---:|---|
| `packages/api/service/main.py` | **1550行** | FastAPI app・DTO・全route・SSE・agent/RAG/NL2SQL/minutes/STT が同居 |
| `create_app()` | L257-1547（**1291行**） | route登録と業務ロジックが単一関数 |
| `/api/chat/stream` ハンドラ | L305-779（**474行**） | validation・RAG・agent・画像・会話永続化・監査・SSE が混在 |
| `jetuse_core/chat.py::stream_agent()` | L237-436（**約200行**） | native Responses ReActループ。auto_tools=false承認往復 / true自動実行 |

Frontend:

| 対象 | 実測 | 問題 |
|---|---:|---|
| `packages/web/src/pages/chat.tsx` | **1194行**（`Chat()` ほぼ全体） | 30+ useState・fetch・SSE・ツール承認・履歴・UI が単一関数 |
| `packages/web/src/prefs.tsx` | **703行** | i18n辞書（ja/en・約320キー）と provider/hook が同居 |
| `packages/web/src/pages/dbchat.tsx` | **645行** | dataset/NL2SQL/chart/table UI が集中 |
| `packages/web/src/pages/voicechat.tsx` | **452行** | 録音・STT・TTS・SSE・UI が集中 |

### 推奨分割

> **⚠️補正（監査）— `/api/chat/stream` のagent実行は3経路ある**。分割時はこの3分岐を `agent_dispatch.py` に集約するのが目的:
> 1. 保存済みagent かつ `framework=="select_ai"` → `select_ai_agent.run()`（main.py:441-482、早期return）
> 2. 保存済みagent（その他SDK）→ `hosted_agent.invoke_agent()`（main.py:487-555、早期return）
> 3. `req.agent` のアドホック・モード（未保存・🛠ツールパネル等、reasoningモデル）→ `jetuse_core/chat.py::stream_agent()`（main.py:660、native Responses ReAct）
>
> つまり `stream_agent()` は**現役だが唯一ではない**。保存済みagentの主経路は hosted container / Select AI に移行済みで、`stream_agent()` はアドホック・モード用。

Backend:
- `service/schemas.py`（DTO）/ `service/sse.py` / `service/agent_dispatch.py`（上記3分岐を集約）
- `service/routes/chat.py` / `routes/agents.py` / `routes/rag.py` / `routes/dbchat.py`
- `chat_stream()` を validation / RAG dispatch / agent dispatch（select_ai / hosted / native の3分岐）/ normal stream / persistence-audit に分割。
- `AgentDefinition.validated()` を route schema から service層 validator へ移す（テスト容易化）。

Frontend:
- `src/lib/sse.ts`（§3）/ `src/lib/api.ts`
- `src/pages/chat/useChatStream.ts` / `useConversations.ts` / `MessageList.tsx` / `Composer.tsx` / `ToolPanel.tsx`
- `src/i18n/dict.ja.ts` / `dict.en.ts`（prefs.tsx から辞書を分離）

### ⚠️順序の注意

フロントは現状**ユニットテスト0**（§7）。`chat.tsx` の大型分割は高リスクなので、**先に §3 のSSEヘルパー＋chat payload構築のVitestテストを入れてから**分割する。`main.py` 分割も pytest を緑にしてから。

---

## 6. 補足: 巨大クラス / 循環依存

- 問題になる巨大クラスはほぼ無し。最大は `jetuse_core/settings.py::Settings` と `service/main.py::AgentDefinition`（いずれも DTO/settings 的役割）。
  - ただし `AgentDefinition.validated()` は framework 移行の仕様差分を抱えるため service層へ移すと良い（§5）。
- 循環依存: README は Python/TS とも無しと報告（⚠️本検証では再確認していないが影響小のため優先度低）。

---

## 7. テスト不足

### Backend（✅）
- API テスト125件中 **7件失敗**（§1）。失敗集中: agent framework 値の互換性 / hosted routing と旧 in-process 期待の混在 / tool validation 差分 / DB依存。
- 改善: ADR-0009準拠へ更新、503(未設定)と422(validation)の分離、coverage下限導入。

### Frontend（✅ 深刻）
- `packages/web/src` に unit/spec **無し**。CIは `npm run lint` と `npm run build` のみ。
- 優先追加: ① `ucform` のテンプレ描画/必須バリデーション ② 共通化後のSSE parser ③ auth header / 401 reauth ④ chat送信payload構築 ⑤ tool承認の状態遷移。
- Vitest 導入 + Playwright smoke を主要画面に（CIか手動検証）。

### Infra/Ops（✅）
- CI に `terraform fmt -check -recursive` と `validate` はあるが、`ops/*.sh`/`*.py` の smoke / shellcheck なし。
- 改善: `shellcheck ops/*.sh`、module単位 `terraform validate`、ops Python の import/parse smoke。

---

## 8. 追加提案（README 未記載・⚠️監査で候補を補正）

- **バンドル分割**: `npm run build` の **805KB main chunk** 警告は遅延読込で削減でき、初期表示が軽くなる。P2相当。
- ただし **mermaid と chart.js は既に dynamic import 済み**（`components/markdown.tsx:66` `await import('mermaid')`、`components/resultchart.tsx:39` `await import('chart.js')`）。本doc初版の「mermaid/chart を dynamic import」提案は不要。
- 次に見るべき実効候補:
  - Markdownスタック（`react-markdown` / `remark-gfm` / `rehype-highlight` / `lowlight` / `katex`）の遅延ロード。チャット等、必要画面でのみ読み込む。
  - route/component 単位の `React.lazy`（design/gallery 系、admin、dbchart 系の route chunk 分割）。

---

## 9. 推奨ロードマップ（検証反映・調整版）

| Phase | 内容 | リスク | 根拠 |
|---|---|---|---|
| **P0** | CI green化: テストをADR-0009準拠へ更新（langgraph auto_tools 期待も再定義）＋DB依存テスト隔離 | 低 | mainが現在赤。§1 |
| **P0.5** | デッドコード削除(2-A 即削除可): get_owner_tables、delete_owner、gallery Nav、vite.svg、test.txt | 極低 | 参照0・他依存なし。§2-A |
| **P0.6** | 旧framework値のDBデータ移行（分布確認→migration→normalize一元化方針の明文化） | 低 | DB DEFAULT 'native' + 後方互換normalize実在。§1.5 |
| **P0.7** | デッドコード削除(2-B): agents_sdk+langgraph_engine をテスト/`sdk_*`残骸/docs更新とセットで除去 | 低 | テスト・相互依存あり。§2-B |
| **P1a** | SSE共通化 `lib/sse.ts` ＋ そのVitestテスト | 低 | 8ファイル重複。§3/§7 |
| **P1b** | `jetuse_shared` でSSRF/SQLサニタイズ等の二重管理解消 | 中 | 定数乖離など実害。§4 |
| **P1c** | 大型分割（main.py / chat.tsx）— **テスト整備後**。agent_dispatchは select_ai/hosted/native の3分岐 | 中〜高 | §5。安全網が前提 |
| **P2** | フロントVitest本格導入 / Playwright smoke / build分割（Markdown系・route lazy） | 低〜中 | §7/§8 |

> 元の Phase 構成（README §推奨ロードマップ）とおおむね一致。差分は「デッドコード削除を2群に分割し旧engine群は P0.7 でテスト/docsとセット化」「**旧framework値のDB移行 P0.6 を新設**」「大型分割の前にテスト整備を必須化」「コンテナ共通化は `jetuse_shared` 限定」「build分割の候補をMarkdown系/route lazy へ補正」。

---

## 付録: 主要な file:line 早見

- 検証ロジック: `service/main.py:155-188`（`AgentDefinition.validated`。langgraph+auto_tools の拒否ルールは**無い**）
- agent実行3経路: `service/main.py:441-482`(select_ai) / `487-555`(hosted) / `660`(stream_agent=アドホック)
- 巨大: `service/main.py:257-1547`(create_app) / `305-779`(/api/chat/stream) / `jetuse_core/chat.py:237-436`(stream_agent)
- 旧framework後方互換: `jetuse_core/hosted_agent.py`(`_LEGACY_SDK`/`normalize_sdk`) ⇔ 使用 `service/main.py:488` / DB既定 `migrations/010_agent_framework.sql:1`(DEFAULT 'native') / フロント `pages/agentbuilder.tsx:55-63`,`pages/agents.tsx`
- デッド(2-A即削除): `datasets.py:236`(get_owner_tables), `rag_opensearch.py:133`(delete_owner), `components/gallery.tsx:49`(Nav), `assets/vite.svg`, `test.txt`
- デッド(2-B要セット): `jetuse_core/agents_sdk.py`(被参照 `tests/test_agents_sdk.py:46`), `jetuse_core/langgraph_engine.py:20`(`from .agents_sdk`)
- 重複(ツール): `agent_common.py:80,153,84-93,125-150,154-189,214-222` ⇔ `jetuse_core/tools.py:25-64,87-97`, `jetuse_core/webtools.py:26-42,74-106`
- 重複(NL2SQL): `agent_db.py:79-101,104-106,109-118` ⇔ `jetuse_core/nl2sql.py:43-68,159-163,166-177`
- SSE重複: chat.tsx:380 / dbchat.tsx:198 / rag.tsx:138 / usecase.tsx:99 / minutes.tsx:143 / voicechat.tsx:117,316 / realtime.tsx:108 / video.tsx:114
- 遅延ロード済(分割対象外): `components/markdown.tsx:66`(mermaid), `components/resultchart.tsx:39`(chart.js)
- CI: `.github/workflows/ci.yml:22`(ruff) `:24`(pytest)
