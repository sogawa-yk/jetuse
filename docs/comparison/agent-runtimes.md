# 比較: エージェント実行ランタイム(hosted SDK 3種 vs Select AI Agent)

AWS版との差分のエージェント基盤として、本プロトタイプは2系統4種の実行ランタイムを持つ。
作成画面の「SDK/種別」選択でルーティングする(ADR-0009 / ENH-04)。

| 種別 | 実行場所 | 仕組み | ツール | 向くユースケース |
|---|---|---|---|---|
| OpenAI Agents SDK | Hosted Application(コンテナ) | 汎用ReAct。tools/promptをステートpush | web/fetch/time/rag/NL2SQL(コンテナ内蔵) | 業界標準SDKでの一般エージェント |
| Google ADK | 同上 | 同上(カスタムBaseLlm) | 同上 | ADK資産・マルチエージェント志向 |
| LangGraph | 同上 | 同上(create_react_agent) | 同上 | グラフ/分岐志向 |
| **Select AI Agent** | **ADB内(DBネイティブ)** | DBMS_CLOUD_AI_AGENT(agent/tool/team)+RUN_TEAM | DB内SQL(Select AIプロファイル) | **社内DBへの定型分析・データ密着**。NL→SQL→実行がDB内で完結 |

## 使い分け指針
- **Webや汎用ツール、外部MCP、複数SDK比較**が要る → hosted SDK 3種(コンテナ)。
- **社内データベース(SH/アップロードCSV)への分析が主**で、DB内で完結させたい/データを外に出したくない
  → **Select AI Agent**。NL2SQLとガバナンスがDB側に集約され、レイテンシ/データ局所性に優れる。

## 実機根拠
- hosted 3種: docs/verification/jetuse-app/AGT-MULTI.md / SPIKE-ADK.md。
- Select AI Agent: docs/verification/spikes/SPIKE-E1.md / ENH-04.md(ADB 26ai DBMS_CLOUD_AI_AGENTで
  agent/tool(SQL)/task/team→RUN_TEAMを実機確認。販売チャネル別売上を集計回答)。
