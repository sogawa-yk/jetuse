# タスク: PLG-04 中央レジストリ Service（MVP）

## ゴール
全インスタンスから参照できる共有レジストリ本体を構築する（D2）。読取API＋publish API＋index。

## 対象 area
api（packages/registry）＋ infra

## 依存
PLG-01

## 仕様参照
docs/enhance/202607-demo-platform-plan.md §6 / docs/comparison/marketplace-plugin.md §2 / specs/16-platform.md

## 受け入れ条件（検証可能な述語で書く）
- [ ] packages/registry が list / search / get / download / publish API を提供
- [ ] Object Storage を保存層とし、publish 時に index.json を更新
- [ ] 発行者認証＋公開鍵登録＋publish 時の署名検証を実装
- [ ] 無署名の publish を拒否する
- [ ] publish→index更新→list/get/download の統合テストが成立
- [ ] infra/terraform/modules/plugin-registry が `terraform plan` クリーン（apply はしない）
- [ ] docs/verification/PLG-04.md に実行ログを残す

## 成果物
packages/registry / infra/terraform/modules/plugin-registry / tests / docs/verification/PLG-04.md

## 非ゴール / 制約
- μService 高度化（評価・DL数・レビュー）はステージ4。本タスクは MVP（Object Storage + index）。
- 大きい場合は 04a=API / 04b=Terraform に分割してよい。
- 人間ゲート: Terraform apply・課金リソース作成（エージェントは plan まで）。
- 認証情報・OCID をコミットしない。コミット/PR/push は人間承認後。
