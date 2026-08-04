# ADR-0007: OpenAI Agents SDKはChatCompletionsModel経由で使う（Responses直結は不可）

日付: 2026-06-12
状態: 承認済み（FW-01実装で採用、実機検証済み — docs/verification/jetuse-app/FW-01.md / spikes/spike12_agents_sdk.py）

## 決定

OpenAI Agents SDK（openai-agents 0.17.x）をOCI Enterprise AIで使う際は、
**SDK既定のResponsesモデルではなく `OpenAIChatCompletionsModel` を明示指定する**。

```python
Agent(..., model=OpenAIChatCompletionsModel(model="openai.gpt-oss-120b", openai_client=oci_client))
```

この構成で 基本実行 / function tool / handoffs / guardrails / streaming のSDK全機能が動作する。

## 背景 — Responses APIが使えなかった原因（実機で確定）

### 原因1（本質）: SDKが送るinput形式とOCIの厳格スキーマの不整合

OCIのResponsesエンドポイントが受理するinputは次の2形式**のみ**（4パターンの対照実験で確定）:

| inputの形式 | OCI | 備考 |
|---|---|---|
| A. 素の文字列（`input="..."`） | ✅ | |
| B. `{"role": "user", "content": "..."}` のアイテム列 | ❌ `Invalid 'input'` | **SDKが送る形式**（EasyInputMessage） |
| C. `{"role": ..., "content": [{"type": "input_text", ...}]}`（type:"message"なし） | ❌ `Invalid 'input'` | |
| D. `{"type": "message", "role": ..., "content": [{"type": "input_text", ...}]}` | ✅ | 本アプリのnativeパスが使う形式（CHAT-01の既知quirk） |

Agents SDKは入力（文字列であっても）を内部でBの簡易アイテム列へ変換してから
`responses.create` に渡すため、OCIでは必ず `400 invalid_value: "Invalid 'input': expected a
valid Responses API input payload."` になる。OpenAI本家はB/Cを寛容に受理するためSDK側に
不具合はなく、**OCI互換レイヤーの厳格バリデーションとの相性問題**である。

### 原因2（誤誘導で切り分けを妨害）: OpenAi-Projectヘッダ欠落時のエラーメッセージ

`OpenAi-Project` ヘッダを付けずにResponsesを呼ぶと、OCIは
**「Compartment ID must be provided.」を返す**。CompartmentIdヘッダを付けていても出るため
「ヘッダが届いていない」方向の調査に誘導される（今回も切り分けに時間を要した）。
実際の不足はProjectヘッダであり、付与すると原因1の本当のエラーが現れる。

> `OpenAi-Project` ヘッダの値 = **OCI Enterprise AIのプロジェクトOCID**
> （CLI/APIのリソース名は `GenerativeAiProject`、`oci generative-ai generative-ai-project create`
> で作成。OCIDは `ocid1.generativeaiproject...`）。Conversations / Files / Vector Store /
> 長期・短期メモリを束ねる分離単位で、本アプリでは `jetuse-dev-project`（`.env` の
> `PROJECT_OCID`）。OpenAI本家の "Projects" に相当（SPIKE-03/05）。

### 原因ではなかったもの

- openaiパッケージのバージョン（1.x→2.41で挙動変化を疑ったが、2.41.1で形式Dは正常動作）
- httpx非同期クライアント / IAM署名（署名・ヘッダとも正しく送信されていることをイベントフックで確認）

## 検討した代替案

| 案 | 評価 |
|---|---|
| SDKのModelをサブクラス化しB→D変換を挟む | Responses限定機能（hosted tools等）が使える可能性はあるが、SDK内部実装への依存が強く更新で壊れやすい。v1では見送り（必要になれば再評価） |
| SDKの入力変換をモンキーパッチ | 同上より更に脆い。不採用 |
| **ChatCompletionsModel（採用）** | SDK公式サポートの構成。OCIのchat completionsは寛容で、gpt-ossを含む全登録モデルが動作（gpt-ossがchat completionsでも動くことはFW-01で新規確認） |

## 採用構成の付帯条件（実機で確定したもの）

1. `set_tracing_disabled(True)` 必須 — SDKはトレースをOpenAI本家へ送ろうとする
2. function callingの引数JSONが崩れることがある（gpt-ossで空キーを実測）→
   ツールラッパーでスキーマ外キーを除去する寛容化を入れる（jetuse_core/agents_sdk.py）
3. `FunctionTool(strict_json_schema=False)` — OCI chat completionsにstrictスキーマ前提を持ち込まない

## 影響・制約

- Responses限定機能（code_interpreter / file_search / server-side MCP / OCI Conversations短期メモリ）は
  Agents SDKパスでは使えない（nativeパスは従来どおりResponses形式Dで利用可能）
- OCI側がB/C形式を受理するようになれば、本ADRの制約は解消されうる（定期的に再検証する価値あり）

## 参照

- 検証レポート: docs/verification/jetuse-app/FW-01.md / スパイク: spikes/spike12_agents_sdk.py
- 関連quirk: docs/tips.md（OpenAi-Project誤誘導エラー、型付きinput必須はCHAT-01から既知）
