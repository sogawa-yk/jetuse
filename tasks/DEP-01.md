# タスク: DEP-01 — 生成デモのコンテナ配備(L3 ホスト型 / Phase 9 基盤再利用)

## ゴール
合成済み・ガバナンス ok のデモ構成(`DemoComposition`)から、**既存の container-instance Terraform
モジュールへそのまま渡せる宣言的なコンテナ配備仕様**を決定的に生成し、その仕様で
`terraform plan` が通る(配備構成=HCL/変数/モジュール結線の妥当性が静的検証される。実在性・実起動の
保証ではない)ところまでを成立させる。新規インフラのプロビジョニングはしない(D8: デプロイ上限=
コンテナ)。実 apply・実 subnet/nsg/image/secret での検証は人間ゲート(plan 止まり)。

## 対象 area
api(主) + infra。test_cmd=`.venv/bin/pytest packages/api/tests` / lint=`.venv/bin/ruff check
packages/api` / infra=`terraform fmt -check && terraform validate && terraform plan`。

## 依存・再利用(新規の実行基盤・認可経路は作らない)
- **ADR-0009**(SDK→ホスト型 Application OCID): `hosted_agent.normalize_sdk` / 設定 `agent_*_app_ocid`。
- **ADR-0011**(OCIR 配布): 配備イメージは OCIR(ap-osaka-1, public)。public のため pull secret 既定不要。
- **ADR-0014 / Platform API ブローカー**(D5): デモコンテナは DB 資格情報を持たず、付与予定スコープ
  (`required_scopes`)のみ仕様に記録する(実注入の本実装は DEP-02)。
- **infra/terraform/modules/container-instance**: 既存モジュールをそのまま consume(再作成しない)。
- base=feat/stage-4。

## 受け入れ条件(検証可能な述語で書く)
- [ ] `jetuse_core/deploy.py`(新規): `build_deploy_spec(composition, ...) -> ContainerDeploySpec` を
      提供。決定的・副作用なし。`ContainerDeploySpec` は container-instance モジュール変数へ 1:1 写像
      でき、`to_tfvars()` / `render_tfvars_json()` / `module_environment()` を持つ。
- [ ] **fail-closed**: `composition.ok=False`、**内部実行する `validate_governance(composition)` が
      ok=False**(report は引数で受けず詐称口を作らない)、`image_url` が非 OCIR(kix・空セグメント含む)/
      未指定、未知 `sdk`、リソース範囲外、env 値への Vault OCID 混入、のいずれでも `DeploySpecError` を
      送出して仕様を作らない。
- [ ] **DEP-01 は秘密(実値も Vault OCID 参照も)を tfvars/state に持たない**。配備仕様は要求秘密の
      **論理名のみ宣言**(`required_secrets`、allowlist 制約)。具体的な Vault OCID 解決・注入は DEP-02。
      非秘密 env はキー名前空間(OCI_REGION/JETUSE_*)＋資格情報名ヒントで秘密の運搬路にしない。
- [ ] **ADR-0009 再利用**: `sdk` 指定に応じ `agent_*_app_ocid` を解決して env に載せる。
- [ ] `infra/terraform/environments/hosted-demo/`(新規): container-instance モジュールを consume し、
      deploy.py 生成の `*.auto.tfvars.json` を流し込む薄い env。`terraform fmt`/`validate` クリーン、
      jetuse-dev で `terraform plan` がエラーなく完了する(apply はしない)。
- [ ] `.venv/bin/pytest packages/api/tests` 全件パス・`.venv/bin/ruff check packages/api` クリーン。
- [ ] 既存の公開シグネチャ(synth/governance/hosted_agent/container-instance モジュール)を壊さない(追加のみ)。
- [ ] **ADR-0015** をドラフト作成(L3 ホスト型/実行基盤・SSO・データ注入の決定案)。状態=提案中(承認は人間ゲート)。

## E2E シナリオ(実環境 / jetuse-dev・最低2本)
完了ゲートで Claude が下記を実行し証跡を `runs/<run-id>/e2e/` に残す。Codex は実行せず証跡＋diff を評価する。
- [ ] **シナリオ1(コンテナ仕様生成)**: 代表構成(SBA-A + Slack active)を `synthesize()` で合成 →
      `build_deploy_spec()` で配備仕様を生成 → `render_tfvars_json()` を出力。**期待**: 生成 JSON に
      実秘密値も Vault OCID も無く(`secret_refs` キー自体が無い)・`environment_variables` は非秘密のみ
      (キーは OCI_REGION/JETUSE_*)・`required_secrets` は論理名のみ・`required_scopes` に
      `platform:connector.invoke` を含む。証跡=生成 JSON ＋ アサーションログ。
- [ ] **シナリオ2(terraform plan 検証)**: シナリオ1 の生成 tfvars を `generated.auto.tfvars.json` に
      配置し、jetuse-dev コンパートメントで `terraform init -backend=false` → `terraform plan`。
      **期待**: plan がエラーなく完了し、container-instance(コンテナ 1 個)が計画され、env に実秘密値が
      現れない。証跡=plan ログ(機微値はマスク)。
- [ ] **SKIPPED**: 実 `terraform apply`(課金・実コンテナ作成)は人間ゲートのため実施しない →
      `runs/<run-id>/e2e/SKIPPED.md` に理由明記。実 Vault secret 作成・実 OCIR push も範囲外として明記。

## 非ゴール / 制約
- DEP-02(Platform API の実注入)・MKT-*(マーケット流通)は本タスク非対象。
- 既存リソース(VCN develop / インスタンス dev / バケット)は参照のみ。新規インフラは作らない。
- `terraform apply` は絶対にしない(plan 止まり)。コミット/PR/push は人間ゲート。
- spec-driven: specs/ にない判断は実装せず docs/decisions/ に ADR 案(ADR-0015)を書く。
