# タスク: PLG-02 インスタンス側データモデル

## ゴール
プラグインのインストール状態を永続化し、取込んだ定義の出所を追跡できるようにする。

## 対象 area
api

## 依存
PLG-01

## 仕様参照
docs/enhance/202607-demo-platform-plan.md §6・§10 / specs/16-platform.md

## 受け入れ条件（検証可能な述語で書く）
- [ ] migration で installed_plugins テーブルを作成（id / plugin_id / version / kind / source_registry / manifest(JSON) / signature_verified / installed_by / installed_at）
- [ ] usecases / agents テーブルに source_plugin_id / source_version カラムを追加
- [ ] jetuse_core/plugins/store.py がインストール記録の CRUD を提供
- [ ] migration 適用がローカルADBで冪等に成功する（再適用してもエラーにならない）
- [ ] store の CRUD 単体テストが全件パス
- [ ] 既存 /api/usecases・/api/agents の取得が後方互換（既存テスト green）

## 成果物
migration / jetuse_core/plugins/store.py / tests

## 非ゴール / 制約
- レジストリ通信・署名検証は PLG-03。UI は含めない。
- 既存リソース（VCN develop / インスタンス dev / バケット jetuse-oci-source-documents）は参照のみ。
- コミット/PR/push は人間承認後。
