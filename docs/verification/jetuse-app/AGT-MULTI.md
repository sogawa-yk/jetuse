# AGT-MULTI: 3SDK別ホスト型ReActエージェント(ADR-0009)検証レポート

実施日: 2026-06-15 / リージョン: ap-osaka-1

## 目的

エージェント実行を完全hosted化し、**OpenAI Agents SDK / ADK / LangGraph** をそれぞれ
Hosted Application(コンテナ)としてReActエージェントにデプロイ。アプリのSDK選択で
リクエスト送り先コンテナを切り替える。tools/promptは焼き込まず**ステートとしてpush**する。

## デプロイ結果(ACTIVE/ACTIVE)

| SDK | OCIRイメージ | Hosted Application |
|---|---|---|
| OpenAI Agents SDK | jetuse-dev-agent-openai:0.1.1 | jetuse-dev-agent-openai |
| LangGraph | jetuse-dev-agent-langgraph:0.1.1 | jetuse-dev-agent-langgraph |
| ADK (Google) | jetuse-dev-agent-adk:0.1.1 | jetuse-dev-agent-adk |

- 旧GAP-04サンプル jetuse-dev-hosted-agent は削除（差し替え）。
- IDCS inbound認証は既存 `jetuse-agent`(audience=jetuse-agent, scope=invoke)を3コンテナで共用。
- API は 0.32.0 で再デプロイ（SDK→コンテナrouting有効化）。

## E2E(実機・直接invoke)

3コンテナへ client_credentials トークンで直接 invoke（contract:
`{system_prompt, enabled_tools, input, history, rag_store_id, model} -> {output, tool_trace, sdk}`）。
`enabled_tools=["get_current_time"]` で ReActのツール実行→回答を確認:

```
[openai]    HTTP 200 sdk=openai_agents tools=['get_current_time']  → 2026年06月15日（月）
[langgraph] HTTP 200 sdk=langgraph     tools=['get_current_time']  → 2026年06月15日（月）
[adk]       HTTP 200 sdk=adk           tools=['get_current_time']  → 2026年06月15日
```

→ **3SDKすべて、プラットフォーム上で resource principal による OCI LLM 呼び出し＋
コンテナ内ツール実行＋ステートprompt が機能**（routingはApplication OCIDで切替）。

## 実機で確定したハマりどころ

- **コンテナは8080でlisten必須**。8000だとデプロイが11分CREATING後 NEEDS_ATTENTION
  （work-request: "timed out before the container was ready"）で失敗。ローカルは8000でも
  /health 200で起動するため切り分け注意。work-requestエラーは
  `GET /20231130/workRequests/{id}/errors`(raw-request)で取得。
- ADK 2.2.0 は Python 3.12 必須・カスタムBaseLlmで接続（SPIKE-ADK）。

## 設計(ADR-0009)の実装

- コンテナ=汎用ReActエージェント。`packages/agent-containers/`(agent_common + server + run_{openai,langgraph,adk})。
- 内蔵ツール: web_search / web_fetch / get_current_time / rag_search。
  **query_database(NL2SQL)はADBウォレット配線が必要なため次段**（コンテナ未搭載）。
- アプリ: framework=SDK選択(openai_agents/adk/langgraph)。`main.py`単一routing、
  in-processエンジン(native/agents_sdk/langgraph)廃止。`hosted_agent.invoke_agent(sdk,state)`。

## 追補(2026-06-15): query_database(NL2SQL)をコンテナ搭載しE2E成功

- `agent_db.py` を3コンテナに追加(イメージ 0.2.0)。SemanticStoreでNL→SQL生成(署名)＋
  JETUSE_QUERY読取専用実行。ウォレットは非公開バケットから resource principal で取得。
- 必要IAM(ユーザーが付与済み): `Allow dynamic-group jetuse-dg to read objects in compartment
  jetuse-proto where target.bucket.name='jetuse-dev-app-data'`。
- **3SDKとも query_database を直接invokeでE2E成功**(販売チャネル別売上の集計を返す):
  ```
  [openai/langgraph/adk] HTTP 200 tools=['query_database']
   → Direct Sales 57,875,260.6 / Partners 26,346,342.32 / Internet 13,706,802.03
  ```
- イメージ更新は「アプリ削除→0.2.0+フルenvで再作成」で実施(in-place更新は不可。tips参照)。
  新APP OCIDをtfvarsへ反映しAPI再デプロイ(0.33.0)。コンテナ内蔵ツールは
  web_search/web_fetch/get_current_time/rag_search/**query_database** の5種。

## 残課題

- アプリUIからのブラウザE2E（SSOは対話操作のため本レポートは直接invokeで代替検証）。
- 旧in-processエンジンモジュール(agents_sdk.py/langgraph_engine.py)の整理(現状dead code)。
