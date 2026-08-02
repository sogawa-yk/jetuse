# specs/11 — Phase 6 エージェント（AGT-01〜05）

状態: ドラフト（2026-06-11作成。AGT-01を先行詳細化）
仕様参照: SPIKE-09（ツール機構の実機確定）/ specs/07（チャット基盤）

## [AGT-01] Function Callingフレームワーク

### ツールレジストリ（jetuse_core/tools.py）

`ToolDef = {name, label(表示名), description, parameters(JSON Schema), handler, requires_approval}`

初期ツール:
1. **web_search**: DuckDuckGo HTML版をスクレイプ（APIキー不要。SSRF対象外=固定ホスト）。上位5件の{title,url,snippet}を返す。built-inのweb_searchはOCIで不可（SPIKE-09）のためカスタム実装
2. **web_fetch**: URL本文抽出（UC-02のwebtools.extract_urlを流用。SSRFガード済み）
3. **code_interpreter**: built-inツールとして透過（OCI側サンドボックス実行。handlerなし）

### 実行フロー（承認2モード）

`/api/chat/stream` 拡張: `agent: bool`（ツール有効化）+ `auto_tools: bool`（自動実行）

- **都度承認モード（既定）**: モデルがfunction_callを出したらSSEで `{"tool_call": {name, label, arguments, call_id}}` を送ってストリーム終了。UIが承認カードを表示 → 承認時 `POST /api/agent/execute-tool` で実行し結果を受領 → UIが履歴+`tool_results`（function_call+output のペア）付きで再度 `/api/chat/stream` を呼ぶ → 続きが streaming される（さらにツール呼び出しがあれば繰り返し）
- **自動実行モード**: サーバー側で function_call → handler実行 → output提出 を**ホップ上限**までループ。途中経過をSSEイベント（tool_call / tool_result）で逐次通知。上限は設定可能（AGT-04。下記「ツール往復の上限」）
- 拒否時: UIが `function_call_output` に「ユーザーがツール実行を拒否しました」を入れて継続（モデルがツールなしで回答）

### ツール往復の上限（AGT-04・ADR-0025）

1 ホップ = モデル 1 往復。業務 API を順に呼ぶ手続き（実案件のデモは API 8 本）は固定 5 ホップでは
入口にも届かなかったため、上限を設定可能にする。

- 既定は `AGENT_MAX_TOOL_HOPS`（既定値 `AGENT_MAX_TOOL_HOPS_DEFAULT`）。要求ごとに
  `max_tool_hops` で上書きできる（エージェント要求以外で指定したら 400。保存済み
  エージェント `agent_id` は別ディスパッチなので 400）
- 上限は**ツールを渡した往復の数**。上限到達時の「ツールなしの最終回答」は上限の外側の
  1 往復（1 要求の最大往復数は上限 + 1）。この往復の `usage` も流す
- **天井** `AGENT_MAX_TOOL_HOPS_CEILING` を超える値は**拒否**する（要求は 422 / 設定値は 400）。
  クランプしない＝「上げたのに効かない」を作らない
- 承認モードの累計ツール結果の上限は `max(16, ホップ上限)`（ホップ上限だけ上げても
  承認モードが先に打ち切られないように連動する）
- **上限に当たったら黙って打ち切らない**: `{"notice": …, "limit_reached": {"reason", "limit"}}`
  を流し、同じ文面を `delta` にも出す（現行 UI は `notice` を描かないため）。
  `reason` は `max_tool_hops` / `max_tool_results`

### 制約・ガード

- ツール実行はサーバー側のみ（クライアントから任意コマンドは渡せない。execute-toolはレジストリのnameと引数のみ受理し、引数はJSON Schemaで検証）
- agentモードはステートレス（全履歴再送。Conversations短期メモリとの統合はAGT-05で再設計）
- 対応モデルはResponses系（gpt-oss）のみ

### UI（チャットページ統合）

- 入力欄に 🛠 トグル（ツール有効化）+ 自動実行チェック
- ツール呼び出しはチャット内にカード表示（名前・引数・[承認して実行][拒否] / 実行済みは結果プレビュー）
- code_interpreter呼び出しは「コード実行中…」表示（built-inのため承認不要、結果はモデル出力に含まれる）

### 完了条件

- [ ] pytest（レジストリ/引数検証/SSEイベント/承認フロー）/ lint / build
- [ ] 実機: 都度承認モードでweb_search→承認→最終回答。自動モードでマルチホップ（search→fetch）完走。code_interpreterで計算質問

## [AGT-01c] ツール拡充（ユーザー承認 2026-06-11）

「ツールが少なくエージェント感がない」フィードバックへの対応。アプリ既存機能をツール化する。

- **rag_search**: ユーザーのRAG文書検索。既定の実体は**file_search built-in**（ユーザーのVector Storeを接続）— 選択時のみtools配列に注入。文書未アップロード時は注入しない（UIに注記）
  - **バックエンド選択（AGT-04・ADR-0025）**: 要求の `agent_rag_backend`（既定 `vector_store` ＝挙動不変 / `adb`）。`vector_store` 以外を指定するときは `enabled_tools` に `rag_search` が要る（無いと検索自体が行われないので 400）。`adb` では `rag_search` を **function tool** として実装し `rag_adb.search`（現行版のみ）を呼ぶ。結果に**チャンク単位の出典**（`source.sheet` / `source.cells` / `version` / `chunk_id`）が載り、`citations` イベントとしても流れる。読み取り専用なので承認は不要（`requires_approval=False`）。adb が使えないときは要求を 400 で断る。built-in 経路は置き換えない（増やすだけ）
  - built-in はホップを消費しないが、`adb` の function tool は**1 回の検索が 1 ホップを消費する**（ホップ上限の既定値はこれを織り込んだ値 — ADR-0025 §1）
- **query_database**: NL2SQL（SQL Search生成→JETUSE_QUERY読取専用実行、既存の多層ガード再利用）。結果は最大20行をJSONで返す。生成に30秒程度かかる旨をdescriptionに明記
- **get_current_time**: 現在日時(JST)。LLMの日付誤りを防ぐ
- **承認パーティション**: `requires_approval=False` のツール（get_current_time/query_database — 読取専用でガード済み）は**都度承認モードでもサーバー側で自動実行**。外部送信を伴うweb_search/web_fetchは従来どおり承認制。安全/要承認が同一バッチに混在した場合は全件承認制にフォールバック（ステートレス継続で安全側の結果が失われるのを防ぐ）
- **MCPプリセット**: 匿名利用可能な公開MCP（deepwiki / Microsoft Learn — 実機確認済み）をUIにワンクリック登録チップとして表示

## [AGT-02] MCPチャット（2026-06-11実装）

SPIKE-11実機確定: Responses APIは **`type:"mcp"` ツール（リモートMCPのサーバーサイド実行）対応**。`mcp_list_tools`/`mcp_call` アイテム、`require_approval:"always"` で `mcp_approval_request` → 継続inputに「元のuserメッセージ+承認アイテム+`mcp_approval_response`」で実行継続（userメッセージ必須）。

### 設計

- 登録: `MCP_SERVERS(id, owner_sub, label, url, auth_secret_ocid NULL, created_at)`（migration 006、owner分離）。CRUD `GET/POST/DELETE /api/agent/mcp-servers`
- **認証情報はVault保存**（plan指定）: トークン付き登録時はアプリがVault secretを作成し OCIDのみADBへ。**現行ポリシーはsecret-family readのみ**のため、書き込みには `manage secret-family` の追加（人間作業 — docs/setup/iam.md追記）が必要。それまでは認証なしサーバーのみ登録可（トークン入力時は明示エラー）
- 実行: ツール選択パネルに登録済みMCPサーバーが `mcp:{id}` として並ぶ。選択時、stream_agentがtools配列に `{"type":"mcp", server_label, server_url, require_approval, headers?}` を追加
  - 都度承認モード: `require_approval:"always"` → `mcp_approval_request` をtool_callイベント（kind=mcp）でUIへ → 承認カード → 継続時に `mcp_approval_response` を構築
  - 自動実行モード: `require_approval:"never"`（サーバーサイドで完結）。`mcp_call` は通知イベントとして表示
- URLバリデーション: https必須 + SSRFガード（webtoolsの公開ホスト検証を流用）

### 完了条件

- [ ] pytest / lint / build
- [ ] 実機: 公開MCPサーバー（deepwiki）を登録→選択→承認モードで承認→回答 / 自動モードで完結
## [AGT-03] Agent Builder（2026-06-11実装）

### エージェント定義

`AGENTS(id, owner_sub, name, description, icon, instructions CLOB, model, enabled_tools(JSON), mcp_server_ids(JSON), project_ocid NULL, visibility, tags, created_at, updated_at)`（migration 007）

- **instructions**: エージェントの人格・役割（system扱いで先頭付与、UIからは変更不可）
- **Project割当（記憶分離）**: `project_ocid` 指定時、そのエージェントの会話・短期/長期メモリは指定Projectに分離される（SPIKE-05: Project間ハード分離）。未指定は既定（jetuse-dev-project）。選択肢は `GET /api/agents/projects`（コンパートメント内のACTIVEなGenerativeAiProject一覧）。**プロジェクト自体の新規作成はUI対象外**（LTM等は作成時のみ設定のためIaC/CLI管理 — ADR-0006教訓）
- **公開共有**: visibility=publicで他ユーザーも利用可。ただし**MCPサーバーはエージェント所有者の私有資源のため共有時は除外**（実行ユーザー自身のMCPは別途選択可）
- 検証: model存在、enabled_toolsはレジストリ名のみ、mcp_server_idsは所有チェック

### チャット統合

- `ChatRequest.agent_id`: 指定時にサーバー側で定義を解決し、instructions（system）・モデル・ツール・MCP・**Project**を適用。可視性チェック（owner or public）
- Project override: `make_inference_client(project_ocid=...)` を会話作成・responses呼び出しに伝搬
- UI: ホームに「エージェント」カード（組み込みユースケースと同様）→ `/chat?agent={id}` で起動。チャット画面はエージェントバッジ表示、モデル/ツール選択はエージェント定義で固定

### ビルダーUI

`/agents/new` `/agents/{id}`: 基本情報・instructions・モデル・ツール/MCPチェック・プロジェクト選択・公開設定。ホームから導線

### 完了条件

- [ ] pytest / lint / build
- [ ] 実機: エージェント作成→チャットでinstructionsが効く→公開共有の可視性→**Project分離**（別Project割当エージェントの会話の長期メモリが既定側に漏れない）
## [AGT-04] Applications/Deployments（2026-06-12実機検証）

OCI Enterprise AIの**ホスト型アプリケーション**: OCIRのコンテナイメージを application（設定）+ deployment（イメージ活性化）の2リソースでマネージドホスティングし、IDCS OAuthで保護されたHTTPS invokeエンドポイントを得る。

- サンプル: `packages/hosted-agent-sample/`（LangGraph 2ノードグラフ + FastAPI、リソースプリンシパルでgpt-oss-120b呼び出し）
- 手順: `ops/deploy-hosted-agent.sh`（build→push→application→deployment→監視）
- 実機確定: inbound-auth-configは必須かつIDCS一択 / 環境変数typeはPLAINTEXT / deploymentのlifecycle-stateにCLI未知enum（NEEDS_ATTENTION）あり→raw-requestで監視 / イメージpullに `read repos` ポリシー+動的グループ追加が必要（適用済み・反映5〜10分） / **invoke URL（未文書）= `https://inference...oci.oraclecloud.com/20251112/hostedApplications/{OCID}/actions/invoke/{パス}`、IDCS Bearer認証**
- 状態: **完了（2026-06-12 invoke E2E成功・6.3秒・リソースプリンシパルでLLM呼び出し動作）**。アプリ統合（エンドポイントをツールとして呼ぶ）は**Phase 9（エージェントFW対応）の実行基盤**として実施
- 詳細: docs/verification/agt-04.md

### [PORT-03] 公開スタックからの配備（2026-07-29 実装）

公開 ORM スタックが 3SDK のホスト型エージェントを**手動作業ゼロ**で配備する。
配備条件は `enable_hosted_agents=true` かつ **認証有効**（OAuth の発行元が要る）かつ
**デプロイ先が大阪 / シカゴ**（エージェント画像の push 先かつ GenAI 実証済み）。
条件を満たさない構成では作らず、`GET /api/health` の `capabilities.agents` が
`disabled`（意図的な未配備。全体の `ok` には影響させない）を返す。
配備したのに設定が欠けている場合だけ `unavailable`（＝故障）とする。

**作成手段は暫定的に OCI CLI を使う。provider が修正されたら標準 resource へ戻す。**

- 理由と戻す条件・手順は **ADR-0019 の追記節が正本**。要約すると、oracle/oci 8.24.0 の
  `oci_generative_ai_hosted_application` は work request の完了判定を誤り（`HOSTED_APPLICATION`
  と `hostedapplication` の照合ミスマッチ）、リソースが正常に作られても必ず失敗扱いになり、
  tainted → 削除 → 再作成で収束しない。
- 現行の構成: 作成・削除は `infra/terraform/modules/hosted-agent/scripts/{ensure,delete}_agent.sh`
  （`oci raw-request`）、OCID の参照は data source `oci_generative_ai_hosted_applications`。
  作成したリソースには所有者タグ `jetuse-owner` と設定指紋 `jetuse-config` を付け、
  **タグが一致しないリソースには触れない**（同名の既存リソースを取り込まない・削除しない）。
- 戻すときは state 移行（CLI で作った実体は state に無い）を必ず用意する。
- 追跡は **Issue #98**（provider 修正後に標準 resource へ戻す）。

その他の実機確定事項:

- コンテナ設定は **`JETUSE_AGENT_CONFIG`（JSON オブジェクト1本）** で渡し、コンテナ側
  `agent_env.py` が `os.environ` へ展開する。1変数ずつ渡すと**値に引用符が残る**
  （provider が JSON 検証だけしてアンマーシャルせず送り、API も verbatim 保存するため）。
- API / Functions ルーター / 3SDK エージェントの画像は**単一の `image_tag`** で揃える。
  invoke ステート（`project_ocid` 等）の契約を共有するため、別リリースに割ってはいけない。
- runtime Dynamic Group には `generativeaihostedapplication` /
  `generativeaihostedapplicationiam` / `generativeaihosteddeployment` の3種と、
  `read repos` / `read vss-family` が要る（公式の配備要件）。
- ACTIVE な Deployment は直接削除できず、Application 削除でカスケードされる。
- コールドスタート（`min_replica=0`）は 35分アイドル後で 5.5〜6.4 秒。
  `hosted_agent.py` の httpx timeout（180秒）で足りる。
- 検証: `docs/verification/PORT-03.md`
## [AGT-05] 長期メモリ統合【必須】（2026-06-11実装）

SPIKE-10b（ユーザー指摘による再調査）で実機確定した方式:

- **プロジェクト作成時にLTM有効化が必須**（後から変更不可と明示エラー）。`jetuse-dev-project` を LTM（extraction=gpt-oss, embedding=cohere.embed-v4.0 ※モデルは「名前」指定・embed-multilingual-v3.0不可）+ STM condenser(gpt-oss) + retention（会話720h/レスポンス168h）で新設し、アプリを移行
- **`memory_subject_id` を会話metadataに付与**（responsesのパラメータではない）。アプリは会話作成時に JWT sub を設定 → 同一ユーザーの全会話で記憶共有。抽出は非同期（公式例で10秒待ち）
- subject間の分離は実機確認済み。Project間も分離（SPIKE-05）
- 移行: 旧プロジェクトの oci_conversation_id は全てNULL化（次の発話で新プロジェクトに遅延再作成）。RAGファイルはOS原本から新プロジェクトへ再取り込み
- ガバナンス: retentionはプロジェクト設定で対応済み。**記憶の個別削除APIは未発見**（残課題: docs調査+必要ならsubjectローテーションで代替）
- 完了条件: アプリ経由で会話Aの事実を会話Bで想起。別ユーザーに漏れない
