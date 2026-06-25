# タスク: SBA-03 コアアプリ SBA-B「在庫・受発注照会」(NL2SQL)

## ゴール
SBA-02 で確立した型に沿い、自然言語で業務DBを照会し結果をグラフ化するサンプルアプリを用意する。

## 対象 area
both（api ＋ web）

## 依存
SBA-02

## 仕様参照
docs/enhance/202607-demo-platform-plan.md §6（SBA-B）/ specs/10-dbchat.md / specs/16-platform.md

## 受け入れ条件（検証可能な述語で書く）
- [ ] 在庫／受発注の業務DBスキーマ＋シードデータを用意
- [ ] NL2SQL 照会UI を SBA-B に組込
- [ ] 結果のグラフ化（既存 Chart）を組込
- [ ] 日本語の照会→生成SQL→読取専用実行→グラフ表示が実環境で動く、を docs/verification/SBA-03.md に記録
- [ ] build / test / lint がパス

## 成果物
sample-app SBA-B 定義＋DBシード / UI / docs/verification/SBA-03.md

## 非ゴール / 制約
- SELECT 以外拒否・行数上限・タイムアウトの既存ガード（SQL-02）を流用、緩めない。
- 既存リソースは参照のみ。コミット/PR/push は人間承認後。
