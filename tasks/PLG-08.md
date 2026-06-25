# タスク: PLG-08 MVP E2E 実機検証（インスタンス間共有）

## ゴール
「公開物を別インスタンスから参照・インストール・実行できる」を実環境で証明する（実機検証主義）。

## 対象 area
docs（検証）＋ 運用

## 依存
PLG-04, PLG-05, PLG-06, PLG-07

## 仕様参照
docs/enhance/202607-demo-platform-plan.md §10 / specs/16-platform.md

## 受け入れ条件（検証可能な述語で書く）
- [ ] インスタンスA（または Project A）で宣言型UCを公開→中央レジストリに掲載される
- [ ] インスタンスB（または Project B）でマーケットからインストールできる
- [ ] B で当該UCが実行でき SSE 出力まで動く
- [ ] 上記の実行ログを docs/verification/PLG-08.md に添付

## 成果物
docs/verification/PLG-08.md

## 非ゴール / 制約
- 新規課金リソースの apply は人間承認後。
- 人間ゲート: デモ承認（ステージ1 出口判定）。
