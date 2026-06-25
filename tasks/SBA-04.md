# タスク: SBA-04 コアアプリ SBA-C「営業案件管理」(エージェント複合)

## ゴール
複数のAI機能（議事録要約・次アクション提案・売上集計・メール下書き）が1アプリ内で連動する複合デモを用意する。

## 対象 area
both（api ＋ web）

## 依存
SBA-02（＋既存の議事録 VOICE-01 / エージェント AGT-01..03 機能）

## 仕様参照
docs/enhance/202607-demo-platform-plan.md §6（SBA-C）/ specs/11-agents.md / specs/12-voice.md

## 受け入れ条件（検証可能な述語で書く）
- [ ] 案件データモデル＋シードデータを用意
- [ ] 議事録要約・次アクション提案（エージェント）・売上集計（NL2SQL）・メール下書きを組込
- [ ] これらの複合AI機能が SBA-C 内で連動して動く、を docs/verification/SBA-04.md に記録
- [ ] build / test / lint がパス

## 成果物
sample-app SBA-C 定義＋シード / エージェント定義 / docs/verification/SBA-04.md

## 非ゴール / 制約
- 外部送信（メール実送信）はモック／承認制。実送信は行わない。
- 既存リソースは参照のみ。コミット/PR/push は人間承認後。
