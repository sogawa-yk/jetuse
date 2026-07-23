# ADR-0008: エージェント実行エンジンの標準をOpenAI Agents SDKにする

日付: 2026-06-12
状態: 承認済み（ユーザー指示「業界スタンダードなのでできればAgents SDKを使いたい」を受けFW-01bで実装）

## 決定

- **新規エージェントの既定エンジンを `agents_sdk`（OpenAI Agents SDK）にする**（ビルダーの初期値）
- フルスクラッチ実装（`native`、Responses API上のReActループ — AGT-01）は選択肢として温存する

## 背景

FW-01時点のSDKパスは「自動実行のみ・MCP/rag_search/code_interpreter非対応」の暫定だった。
標準化の判断にあたり、nativeパスとの機能ギャップ3点をすべてOCI実機で埋められることを確認した（FW-01b）:

| 機能 | 実現方法（実機検証済み） |
|---|---|
| **承認フロー** | `FunctionTool.needs_approval` + `RunState.to_json/from_json` のシリアライズで、ステートレスHTTP往復の中断→承認→再開を実現（状態は約7〜10KB/回） |
| **MCP** | SDKの `MCPServerStreamableHttp` / `MCPServerSse`（クライアントサイド実行。URLが `/sse` 終端なら旧SSE型と判定）。deepwikiでlist_tools→ツール実行まで確認 |
| **rag_search** | Responses限定のfile_search built-inの代替として、**DPホストの `vector_stores.search`** をfunction tool化（CPホストは404 — DP側にのみ存在することを実機確認） |

## 残存ギャップ（native限定のまま）

- **code_interpreter**: Responses hosted tool（OCIサンドボックス実行）。chat completionsに等価機能が
  ないためSDKパスでは利用不可（422でガード）。コード実行が必要なエージェントはnativeを選ぶ
- チャット画面のアドホックツールモード（保存しないエージェント、🛠トグル）は当面nativeのまま
- 短期メモリ（OCI Conversations）は両パスとも非統合（エージェントモードは元々ステートレス）

## 影響

- 承認往復のプロトコルが2系統になる: native=クライアントがツール実行して結果返送 /
  SDK=クライアントは可否のみ返送（`sdk_state`+`sdk_approvals`）しサーバーが再開実行。
  UIは同一の承認カードで両対応
- `sdk_state` はチャットリクエストで往復するため、リクエスト上限(2MB)に余裕を持たせた
- FW-04（比較資料）では本ADRの「どちらを選ぶか」観点（code_interpreter要否・カスタムループの自由度）を整理する

## 参照

- 原因分析（Responses直結不可・ChatCompletionsModel採用）: ADR-0007
- 検証: docs/verification/jetuse-app/FW-01.md（FW-01b追補）、spikes/spike12_agents_sdk.py
