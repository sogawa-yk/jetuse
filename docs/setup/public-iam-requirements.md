# JetUse Public版 IAM要件

JetUse Public版は、IAMとアプリ本体を1つのOCI Resource Manager Stackからデプロイする。実行ユーザーの権限と既存IAMに合わせて、Stack内のIAM作成範囲を選択する。

Terraform実装の詳細は [IAMガイド](./iam.md)、操作手順は [Resource Managerガイド](./orm.md) を参照する。

## 役割

| 役割 | OCI IAMユーザー | 必要な権限 |
|---|---:|---|
| JetUseエンドユーザー | 不要 | 作成されたOIDCユーザーだけ |
| テナンシ管理者としてDeploy | 必要 | Dynamic Group / Policy / Domain / ORM / アプリリソース管理 |
| 専用コンパートメント管理者としてDeploy | 必要 | 専用コンパートメントのORM / アプリリソース管理。テナンシIAMは事前作成 |
| Container Instance / Functions / ADB | Resource Principal | Stackまたは管理者が作成したRuntime Policy |

## 権限別のStack設定

| 実行ユーザー | `enable_dynamic_group` | `enable_runtime_policy` | 事前作業 |
|---|---:|---:|---|
| テナンシIAM管理者 | `true` | `true` | なし |
| Dynamic Groupを作れないが対象コンパートメントのPolicyを管理可能 | `false` | `true` | Dynamic Groupとnamespace参照Policyを作成 |
| IAMを変更できない | `false` | `false` | Dynamic Groupと全Runtime Policyを作成 |

`enable_auth=true`でIdentity Domainを作成する場合は、上表とは別にDomain管理権限が必要。

## Resource Manager実行ユーザー

JetUse専用コンパートメントに限定して、次の権限を付与する。

```text
Allow group <deployer-group> to inspect compartments in tenancy
Allow group <deployer-group> to inspect tenancies in tenancy
Allow group <deployer-group> to read objectstorage-namespaces in tenancy
Allow group <deployer-group> to manage orm-stacks in compartment id <compartment_ocid>
Allow group <deployer-group> to manage orm-jobs in compartment id <compartment_ocid>
Allow group <deployer-group> to manage all-resources in compartment id <compartment_ocid>
```

この権限だけではDynamic Group、root compartmentのPolicy、Identity Domainは作成できない。必要なIAMを事前作成し、対応するStack変数を無効にする。

通常のデプロイ担当者へ`manage all-resources in tenancy`を付与しない。

## Dynamic Group

`<prefix>`はテナンシ内で一意にし、Stackの`prefix`と一致させる。

### Runtime

名前: `<prefix>-runtime-dg`

```text
Any {all {resource.type='computecontainerinstance', resource.compartment.id='<compartment_ocid>'},
     all {resource.type='fnfunc', resource.compartment.id='<compartment_ocid>'}}
```

### Autonomous Database

名前: `<prefix>-adb-dg`

```text
All {resource.type='autonomousdatabase', resource.compartment.id='<compartment_ocid>'}
```

### Semantic Store（SQL Search使用時）

名前: `<prefix>-semantic-store-dg`

```text
All {resource.type='generativeaisemanticstore', resource.compartment.id='<compartment_ocid>'}
```

SQL Searchを使用しない場合は`enable_semantic_store=false`にできる。

## Runtime Policy

JetUse専用コンパートメントの`${prefix}-runtime-policy`には次の権限が含まれる。

- Runtime: Generative AI、Vector Store / File、ADB、Object Storage、Speech、Document、Language、Logging、Monitoring、Secrets
- ADB: Generative AI、Object Storage read
- API Gateway: 同じコンパートメントのFunctions呼び出し
- Semantic Store: DB Tools、Database metadata、Secrets、Generative AI（有効時）

root compartmentの`${prefix}-runtime-tenancy-policy`は次の1文だけを持つ。

```text
Allow dynamic-group <prefix>-runtime-dg to read objectstorage-namespaces in tenancy
```

完全なPolicy文の正本は [IAM Terraform module](../../infra/terraform/modules/iam/main.tf)。

## 管理者への依頼テンプレート

```text
JetUse Public版をOCI Resource Managerからデプロイします。

1. JetUse専用コンパートメント: <name / OCID>
2. デプロイ担当グループ: <domain/group>
3. IAM prefix: <prefix>
4. 実行ユーザーがDynamic Groupを作成できない場合:
   <prefix>-runtime-dg / adb-dg / semantic-store-dg と
   <prefix>-runtime-tenancy-policyを事前作成してください。
5. 実行ユーザーがコンパートメントPolicyを作成できない場合:
   <prefix>-runtime-policyも事前作成してください。

事前作成された範囲に応じて、Resource Manager画面の
enable_dynamic_group / enable_runtime_policyをfalseにします。
```

## 確認項目

- Dynamic GroupのMatching Ruleが対象コンパートメントだけを指している。
- Runtime Policyの各文がJetUse専用コンパートメントに限定されている。
- namespace参照Policyがread 1文だけである。
- Stackの`prefix`と既存Dynamic Group名が一致している。
- IAM反映後5〜10分待ってからresource principalの動作を確認する。

## 公式資料

- [Resource Manager Policy Reference](https://docs.oracle.com/en-us/iaas/Content/Identity/policyreference/resourcemanagerpolicyreference.htm)
- [OCI Generative AI IAM Policies](https://docs.oracle.com/en-us/iaas/Content/generative-ai/iam-policies.htm)
- [Semantic Store Permissions](https://docs.oracle.com/en-us/iaas/Content/generative-ai/semantic-store-permissions.htm)
- [Autonomous Database Resource Principal](https://docs.oracle.com/en-us/iaas/autonomous-database-serverless/doc/resource-principal.html)
