# タスク: PLG-01 プラグイン manifest 仕様 + バリデータ

## ゴール
配布可能なプラグインの manifest スキーマ（L1宣言型サブセット）を確定し、検証ロジックを実装する。

## 対象 area
api ＋ docs

## 依存
なし（ステージ1の起点）

## 仕様参照
docs/enhance/202607-demo-platform-plan.md §4・§6・§10 / 新規 specs/16-platform.md

## 受け入れ条件（検証可能な述語で書く）
- [ ] specs/16-platform.md に manifest 仕様を記述（schemaVersion / id(namespace/name) / version(semver) / kind / name / description / publisher / jetuse.minVersion / requires / permissions / contributes / signature）
- [ ] jetuse_core/plugins/manifest.py が pydantic モデル＋JSON Schema を提供する
- [ ] semver・id 形式・署名フィールドのバリデータを実装
- [ ] `.venv/bin/pytest packages/api/tests/test_plugin_manifest.py` が全件パス（正常系3種＋不正manifest拒否を網羅）
- [ ] docs/decisions/ADR-0013 を起票（3層／中央レジストリ／スナップショット取込／Platform API／ガバナンス4制約／IaC非生成・Galley不採用）

## 成果物
specs/16-platform.md / jetuse_core/plugins/manifest.py / tests / docs/decisions/ADR-0013

## 非ゴール / 制約
- レジストリ通信・UI は含めない。kind は usecase / agent の L1 サブセットに限定。
- 認証情報・テナンシ/コンパートメントOCID・エンドポイント実値をコミットしない。
- spec-driven: 仕様にない判断は実装せず ADR 案を書く。コミット/PR/push は人間承認後。
- 人間ゲート: ADR-0013 の承認。
