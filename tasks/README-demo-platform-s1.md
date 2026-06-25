# ステージ1 タスク索引（デモ生成プラットフォーム）

親計画: [`../docs/enhance/202607-demo-platform-plan.md`](../docs/enhance/202607-demo-platform-plan.md) §10。
各タスクは `LOOP_TASK=<id> GOAL="..." claude` ＋ `/goal` で1本ずつループ実行する（[`../LOOP.md`](../LOOP.md)）。

## 配管トラック（プラグイン公開・インストール）
| ID | 内容 | 依存 |
|---|---|---|
| PLG-01 | manifest 仕様＋バリデータ（ADR-0013 起票） | — |
| PLG-02 | インスタンス側データモデル（installed_plugins） | PLG-01 |
| PLG-03 | レジストリクライアント＋署名検証＋スナップショット取込 | PLG-01,02 |
| PLG-04 | 中央レジストリ Service（MVP, plan まで） | PLG-01 |
| PLG-05 | 公開フロー（builder→export→署名→publish） | PLG-01,04 |
| PLG-06 | マーケットプレイス UI | PLG-03,04 |
| PLG-07 | コントリビューションローダー | PLG-02,03 |
| PLG-08 | MVP E2E 実機検証（横断共有） | PLG-04..07 |

## サンプルアプリ・カタログ・トラック（S1後半〜S2と並行可）
| ID | 内容 | 依存 |
|---|---|---|
| SBA-01 | sample-app 構造定義（scaffold テンプレモデル） | PLG-01 |
| SBA-02 | AI組込FW＋SBA-A 問い合わせ/サポート(RAG) | SBA-01, PLG-07 |
| SBA-03 | SBA-B 在庫・受発注照会(NL2SQL) | SBA-02 |
| SBA-04 | SBA-C 営業案件管理(エージェント複合) | SBA-02 |
| SBA-05 | SBA-D 帳票・経費処理(VLM-OCR, MM-01依存) | SBA-02 |

## 推奨実行順
PLG-01 → PLG-02 → PLG-03 →（PLG-04 は並行可）→ PLG-07 → SBA-01 → SBA-02 →（PLG-05/06、SBA-03/04/05 を並行）→ PLG-08（出口判定）。

## 人間ゲート（停止して承認を待つ）
ADR-0013 承認（PLG-01）／Terraform apply・課金（PLG-04）／デモ品質（SBA-02, PLG-08）／VLM能力前提（SBA-05）。
