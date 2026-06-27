# ステージ3 タスク索引（コネクタ＋Platform API ブローカー）

親計画: [`../docs/enhance/202607-demo-platform-plan.md`](../docs/enhance/202607-demo-platform-plan.md) §7/§6/§9/§10。
基盤前提: specs/16-platform.md §4（`PLATFORM_SCOPES`）／§7 は PAPI-02 で昇格、connector 章は CON-01 で追記。
各タスクは `LOOP_TASK=<id> GOAL="..." claude` で1本ずつループ実行する（[`../LOOP.md`](../LOOP.md)）。進捗キュー: [`STAGE3-PROGRESS.md`](STAGE3-PROGRESS.md)。

## 目的（データ接続基盤）
S1+S2 で「フィールドSAがヒアリングから業務デモを組める」体験が成立した。S3 は、その生成デモ／コネクタ／
（後段の）ホスト型アプリが **DB認証情報を持たずに**テナントデータと SaaS（Slack 等）へ到達する
**Platform API ブローカー（④ / D5・D3前提）** と **コネクタ（L2 MCP）** を整える。越境防止・最小権限・監査をクリーンに担保する。

## タスク
| ID | 内容 | 依存 | area |
|---|---|---|---|
| PAPI-01 | Platform API ブローカー設計ADR＋スパイク（§7） | ステージ2 | api |
| PAPI-02 | スコープ承認＋短期トークン発行（manifest.permissions→付与記録→短期JWT） | PAPI-01 | api |
| PAPI-03 | Platform API 実装（rag.search/db.query(読取)/conversations.read/files.read|write/connector.invoke） | PAPI-02 | api |
| CON-01 | コネクタ(L2 MCP)モデル＋manifest（mcp_servers へ正規化） | ステージ1(PLG-01) | api |
| CON-02 | Slackコネクタ（コア・1本） | CON-01 | api |
| CON-03 | 合成（HBD-03）＋バリデーション（HBD-04）＋ブローカーへの組込＋E2E | PAPI-03, CON-02 | both |

## 推奨実行順
（PAPI-01 と CON-01 を並行）→（PAPI-02 と CON-02 を並行）→ PAPI-03 → **CON-03（出口＝ヒアリング→コネクタ付きデモ起動の一気通貫判定）**。

## 人間ゲート（停止して承認を待つ）
コミット/PR/push（全タスク）／**ADR-0014 承認**（PAPI-01）／spec 昇格（§7=PAPI-02、connector 章=CON-01）／
**Slack 実認証情報の投入**（CON-02）／デモ品質（CON-03）。

## ガバナンス（§4 の4制約を弱めない）
固定リファレンス基盤（触らせない）／制約付きパレット（コネクタはコア=Slack 1本のみ）／合成バリデーション（CON-03 で connector も対象）／
越境防止＝Platform API ブローカー経由・**DB資格情報を渡さない**（D5）。

## 起票予定 ADR
- **ADR-0014（PAPI-01）**: Platform API 認可モデル（スコープ語彙・短期JWT・テナント境界=Project OCID・監査）。計画 §11 で予約済。
