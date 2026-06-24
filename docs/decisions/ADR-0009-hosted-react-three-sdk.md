# ADR-0009: エージェント実行を3つのSDK別Hosted Applicationに集約(hosted専用ReAct)

日付: 2026-06-15
状態: 承認待ち（ユーザー指示で着手。SPIKE-ADK=goを確認済み。3コンテナの本番デプロイは課金リソースのため最終承認制）

## 決定

エージェント実行をアプリ内in-processエンジン(native/agents_sdk/langgraph)から、
**3つのSDK別 Hosted Application コンテナ**に完全移行する。

| SDK選択(UI) | コンテナ | ReActランナー実装 |
|---|---|---|
| OpenAI Agents SDK | jetuse-dev-agent-openai | `OpenAIChatCompletionsModel`(ADR-0008) |
| ADK | jetuse-dev-agent-adk | カスタム`BaseLlm`(SPIKE-ADK) |
| LangGraph | jetuse-dev-agent-langgraph | `create_react_agent`+`ChatOpenAI`(FW-02) |

- エージェント作成画面の「実装(framework)」を **SDK選択**に変更し、選択に応じて
  リクエストの送り先コンテナを切り替える。
- **in-processエンジン(native/agents_sdk/langgraph)は廃止**（ユーザー指示「完全にhosted」2026-06-15）。
  コード実行(Responses built-in)を要するnativeも廃止対象（必要時は別途再検討）。

## コンテキストをステートとする設計（tools/promptを後からpush）

近年のReActは「コンテキスト=ステート」を外から与える流れ。本実装もこれに倣う:

- **コンテナは汎用ReActエージェント**（特定のプロンプト/ツールを焼き込まない）。
- アプリは1リクエストごとに**ステート**を送る:
  `{ system_prompt, enabled_tools: [ツール名...], input, history?, rag_store_id? }`
- **ツール実行はコンテナ内**（「完全hosted」と整合）。ツール実装は3コンテナ共通モジュールとして内蔵し、
  アプリは"どのツールを有効化するか(名前)"と"プロンプト"だけをステートで渡す。
  - 内蔵ツール: web_search / web_fetch / query_database(NL2SQL) / get_current_time /
    rag_search(file_search, store idはステートで受領)。
  - コンテナは resource principal 署名でLLM・VectorStore等にアクセス。DB等の接続情報は
    Hosted Applicationの環境変数(機密はリポジトリ非コミット)。

### 代替案: アプリがツール実行(不採用)
コンテナはプランのみ返し、アプリがtools.pyで実行(承認/監査を中央集約)する案も検討。
分離は綺麗だが「完全hosted」の意図に反し、コンテナ↔アプリの多段往復が増える。今回は不採用。
→ 承認(HITL)/監査の扱いは縮退する点を許容（hosted既定は従来も非承認・非ストリーミング）。

## 認証・配備（GAP-04の方式を踏襲）

- 各コンテナ = 1 Hosted Application + 1 Hosted Deployment。OCIRリポジトリを3つ用意。
- inbound認証: IDCS OAuth(client_credentials, audience/scope)。GAP-04の`jetuse-agent`を共用 or 個別。
- イメージpull: 動的グループ`jetuse-dg`にhostedリソースタイプ追加済み(GAP-04, jetuse-proto対象)を流用。
- invoke URL規則・Bearer取得は`hosted_agent.py`を拡張(複数ターゲット+ステートpush対応)。

## 影響

- `service/main.py` の framework分岐を3コンテナrouting＋ステート構築に置換。in-processエンジン
  (agents_sdk.py/langgraph_engine.py/nativeパス)はエージェント実行からは外す(チャット通常機能のnative
  Responsesは別途。エージェント実行のみhosted化)。
- 既存エージェント定義の`framework`値を新SDK enumへ移行。
- コスト: 常時稼働3アプリ。最小レプリカ運用。
- ストリーミング: hosted invokeは非ストリーミング(GAP-04踏襲)。

## ステータス/次工程
1. 共通ReActコンテナ契約の確定と3コンテナ実装
2. OCIR push → IDCS → Hosted App+Deployment ×3（**課金。最終承認制**）
3. アプリrouting改修＋UI(SDK選択)更新、in-process廃止
4. E2E検証(docs/verification/AGT-MULTI.md) + comparison更新

## 追記（2026-06-18・refactoring P0.7）

インプロセス3エンジンのうち実装済みだった2ファイルを**削除した**:
`jetuse_core/agents_sdk.py`（`stream_agents_sdk` = FW-01）と
`jetuse_core/langgraph_engine.py`（`stream_langgraph` = FW-02）。いずれも production の
エージェント実行経路（`service/main.py`）から未参照で、hosted コンテナ（本ADR）へ
置換済みだったため。検証成果は git 履歴と `docs/verification/FW-01.md`/`FW-02.md` に残る。

**削除しないもの（現役）**: ① framework 値 `langgraph`/`adk`/`openai_agents`/`select_ai`
（hosted ルーティングの分岐キー）② `hosted_agent.normalize_sdk`/`_LEGACY_SDK`
（read-time 正規化。ADR-0010）③ `jetuse_core/chat.py::stream_agent`
（アドホック・モードの native Responses ReAct。保存済みagentの主経路ではないが現役）。

## 参照
SPIKE-ADK / ADR-0008 / ADR-0010 / FW-02 / GAP-04(docs/setup/hosted-agent-oauth.md) / docs/plan-gap-b.md
