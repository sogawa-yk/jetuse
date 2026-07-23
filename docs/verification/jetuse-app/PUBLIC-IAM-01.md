# PUBLIC-IAM-01: Public版 IAM Bootstrap可動テスト

- Date: 2026-07-01
- Target branch: `docs/public-iam-guide`（`origin/main`起点）
- Terraform: 1.15.5
- OCI Provider: 8.20.0
- OCI CLI: 3.85.0
- Test compartment: `jetuse-dev`（OCIDは記録・コミットしない）
- Tenancy home region: `us-ashburn-1`
- Test prefix: `jetuse-spike-iam01`

## 目的

1. Public IAM moduleが必要なDynamic Group / Policyを生成すること。
2. Semantic StoreとDeployer Policyを無効化できること。
3. Deployer権限が専用コンパートメントに限定され、テナンシ全体の `manage all-resources` を含まないこと。
4. 実OCIに対するPlanと、通常利用者にテナンシIAM権限がない場合の挙動を確認すること。

## 1. Terraform契約テスト

実行コマンド:

```bash
terraform -chdir=infra/terraform/modules/iam init -backend=false
terraform -chdir=infra/terraform/modules/iam test
```

結果:

```text
run "full_public_bootstrap_contract"... pass
run "minimal_without_semantic_store_or_deployer_policy"... pass
Success! 2 passed, 0 failed.
```

確認内容:

- Runtime DGはContainer Instances / Functionsだけを含む。
- ADB DGは分離される。
- Semantic Store有効時はDG 1件を追加する。
- Full runtime Policyは22文、Semantic Store無効時は17文。
- API Gateway principal typeは公式例どおり `ApiGateway`。
- Runtime root PolicyはObject Storage namespaceのread 1文だけ。
- Deployer Policyは6文で、`manage all-resources` は対象コンパートメントだけ。
- `manage all-resources in tenancy` が存在しない。

## 2. 実OCI IAM参照テスト

OCI CLIのDEFAULT profileで、ホームリージョンと検証コンパートメントを確認した。

| 操作 | 結果 |
|---|---|
| Home regionの取得 | PASS: `us-ashburn-1` |
| `jetuse-dev` compartmentの参照 | PASS |
| Dynamic Group一覧 | EXPECTED DENY: `404 NotAuthorizedOrNotFound` |
| IAM Group一覧 | EXPECTED DENY: `404 NotAuthorizedOrNotFound` |

通常のデプロイ担当者にはテナンシIAM参照・変更権限を付与しない設計のため、期待どおりの結果。管理者向けBootstrapと通常利用者向けアプリstackを分離する必要性を実環境で再確認した。

## 3. 実OCI Terraform Plan

`create_deployer_policy=false`、`enable_semantic_store=true` とし、実OCIDは環境変数で注入してPlanした。

```text
planned_create_count=5
planned_type=oci_identity_dynamic_group,count=3
planned_type=oci_identity_policy,count=2
```

PlanはPASS。想定外のアプリ・ネットワーク・課金リソースは含まれない。

## 4. 実OCI Apply

この実行ユーザーにはテナンシIAM権限がないため、成功系Applyには管理者承認・権限が必要。現時点では永続的なIAM変更を実行していない。

- OCIリソース作成: 0
- Cleanup対象: なし
- 次のゲート: 管理者が `jetuse-spike-iam01` の5 IAMリソースをApplyすることを明示承認し、Apply後に参照・Destroyまで実施する。

## 判定

| 項目 | 判定 |
|---|---|
| Terraform構文 / provider schema | PASS |
| IAM Policy契約テスト | PASS |
| 実OCI Plan | PASS |
| 通常利用者の権限分離 | PASS（IAM操作が拒否されることを確認） |
| 管理者権限での実OCI Apply / Destroy | PENDING（明示承認と権限が必要） |
