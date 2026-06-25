# タスク: PLG-03 レジストリクライアント + 署名検証 + スナップショット取込

## ゴール
中央レジストリからプラグインを取得し、署名検証のうえスナップショット取込／アンインストールするコアを実装する（D6/D7）。

## 対象 area
api

## 依存
PLG-01, PLG-02

## 仕様参照
docs/enhance/202607-demo-platform-plan.md §6（D6 スナップショット / D7 署名）/ specs/16-platform.md

## 受け入れ条件（検証可能な述語で書く）
- [ ] jetuse_core/plugins/registry_client.py が list / get / download を提供
- [ ] manifest の ed25519 署名を検証する（公開鍵はレジストリ取得。検証失敗時は取込を拒否）
- [ ] スナップショット取込: contributes を版固定でADBへ書き、取込定義に source_plugin_id/version を付与
- [ ] uninstall: 取込んだ定義を除去し installed_plugins から削除
- [ ] モックレジストリに対し install→定義がADBに出現→uninstall→消滅 のE2E単体テストが通る
- [ ] 署名不正 manifest の取込が拒否されるテストを含む
- [ ] `.venv/bin/pytest` が全件パス

## 成果物
jetuse_core/plugins/registry_client.py / tests

## 非ゴール / 制約
- 実レジストリの構築は PLG-04（本タスクはモックで検証）。UI は PLG-06。
- 認証情報・OCID・エンドポイント実値をコミットしない。コミット/PR/push は人間承認後。
