# タスク: SBA-01 サンプル業務アプリの構造定義（scaffold テンプレモデル）

## ゴール
`kind: sample-app` を表現できるようにする（UIテンプレ＋データモデル＋AI組込スロットの定義と取込）。

## 対象 area
api（＋ docs）

## 依存
PLG-01

## 仕様参照
docs/enhance/202607-demo-platform-plan.md §6 / specs/16-platform.md

## 受け入れ条件（検証可能な述語で書く）
- [ ] manifest を kind: sample-app に拡張（screens / データシード / AI組込スロットの定義）
- [ ] scaffold 取込ロジック（sample-app 定義をインスタンスに展開）を実装
- [ ] 合成バリデーションの土台（必要ケイパビリティ／権限スコープの宣言）を持つ
- [ ] sample-app 定義スキーマの検証＋取込の単体テストが全件パス

## 成果物
manifest 拡張 / scaffold 取込ロジック / tests

## 非ゴール / 制約
- 具体的なサンプルアプリ実体は SBA-02 以降。フロント自動生成（Stitch風）はステージ5。
- コミット/PR/push は人間承認後。
