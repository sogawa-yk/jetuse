# SPIKE-ADK: Google ADK を OCI Enterprise AI で動かす実証

実施日: 2026-06-15 / リージョン: ap-osaka-1 / 実行: `/tmp/adk-spike/bin/python spikes/spike_adk_oci.py`

## 目的

3コンテナ化(hosted ReActエージェントを OpenAI Agents SDK / ADK / LangGraph で実装)のうち、
**未検証の Google ADK が OCI 互換API(OpenAI互換+IAM署名)で動くか**を確認する。go/no-go判定。

## 結論: **go(PASS)**

カスタム `BaseLlm` 方式で、ReActのツール呼び出し→ツール結果→最終回答までADK Runnerが完走。

```
[tool_call] get_current_time({})
[tool_result] {'now': '2026-06-15T14:00:00+09:00'}
最終回答: 現在の時刻は 2026年6月15日 14:00（日本標準時）です。
判定: PASS
```

## 実機で確定した要点

- **ADK 2.2.0 は Python 3.12 が必要**（3.9では1.18.0までしか入らず、LiteLLM/MCP拡張も不可）。
  コンテナは `python:3.12-slim`(APIと同じ)で揃える。
- **LiteLlm 方式は不可**: `LiteLlm(model, **kwargs)` は litellm へ静的kwargsを渡すだけで、
  OCIの「リクエスト毎IAM署名(日時+ボディハッシュ)」を満たせない（`extra_headers`は静的）。
  → **カスタム `BaseLlm` サブクラス**で、既存の署名済み `OpenAI`クライアント
  (`httpx.Client(auth=OciResourcePrincipalAuth(), headers={"CompartmentId":...})`)経由で
  chat completions を叩く方式を採用。他SDK(Agents SDK/LangGraph)と同じ「OCI互換chat completionsへ翻訳」戦略。
- **翻訳の要点**: ADKの `LlmRequest`(google.genai types: Content/Part/FunctionCall/FunctionResponse, config.tools=function_declarations)
  ↔ OpenAI chat messages/tools。function_call→assistant.tool_calls、function_response→role:"tool"(tool_call_id一致)。
  ツール実行ループはADK Runnerが駆動するため、BaseLlmは単発翻訳のみでよい。
- `lite_llm` のimportには `pip install google-adk[extensions]` が必要(本方式では未使用だが既定importで踏む)。
- 応答に usage が無く「Skipping missing token usage metadata」警告(無害)。監査でトークン記録するなら
  `stream_options/usage` 付与を別途検討。

## コンテナ設計への反映

3コンテナ共通の「OCI互換chat completionsへ翻訳するReActランナー」を各SDKの作法で実装する:
- OpenAI Agents SDK: `OpenAIChatCompletionsModel`(ADR-0008既存)
- LangGraph: `ChatOpenAI(base_url=OCI, http_client=署名)`(FW-02既存)
- ADK: 本スパイクの **カスタム `BaseLlm`**

いずれもコンテナ内で resource principal 署名(`AUTH_MODE=resource_principal`)。ツール実装はコンテナ内蔵、
アプリは `enabled_tools名 + system_prompt + input(+履歴, RAG store id)` をステートとしてpush(ADR-0009)。

## 参照
- スパイク: `spikes/spike_adk_oci.py`
- 関連: ADR-0008(Agents SDK=chat completions), FW-02(LangGraph), 本件アーキ=ADR-0009
