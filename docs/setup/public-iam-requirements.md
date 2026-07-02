# JetUse Public版 IAM要件（利用者・部門管理者向け）

この資料は、JetUse Public版を **Deploy to Oracle Cloud** で利用する際に「誰にどのOCI IAM権限が必要か」をまとめた提出用チェックリストである。Terraform実装の詳細は [iam.md](./iam.md)、デプロイ手順は [orm.md](./orm.md) を参照する。

利用者の権限に応じて、最初に次のどちらかを選ぶ。

- テナンシ権限がなく、専用コンパートメントの`manage all-resources`を持つ: [専用コンパートメント管理者向けガイド](./public-deploy-dedicated-compartment.md)
- Dynamic Group / Policyを作成できるテナンシ管理者: [テナンシ管理者向けガイド](./public-deploy-tenancy-admin.md)

## 結論

| 役割 | OCI IAMユーザー | 必要なPolicy | Dynamic Group |
|---|---:|---|---:|
| JetUseをブラウザで使うエンドユーザー | 不要 | 不要。JetUse用OIDCユーザーだけ | 不要 |
| Deploy to Oracle Cloudを実行する担当者 | 必要 | JetUse専用コンパートメントのORM・リソース管理権限 | 不要 |
| 初回IAM Bootstrapを実行する管理者 | 必要 | Dynamic Group / Policyを作成できるテナンシIAM権限 | 不要 |
| JetUseのContainer Instance / Functions | Resource Principal | Runtime Policy | 1個 |
| JetUseのAutonomous Database | Resource Principal | Generative AI / Object Storage read | 1個 |
| OCI Generative AI Semantic Store | Resource Principal | DB Tools / Database / Secret / GenAI | 0～1個 |

通常のデプロイ担当者に、テナンシ全体の `manage all-resources`、`manage domains`、`manage policies` は付与しない。管理者が `infra/orm-bootstrap` を一度Applyした後、担当者は `infra/orm` だけをApplyする。

## 構成

```text
テナンシ管理者
  └─ IAM Bootstrap（対象コンパートメントごとに1回）
       ├─ Runtime Dynamic Group
       ├─ ADB Dynamic Group
       ├─ Semantic Store Dynamic Group（任意）
       ├─ Runtime Policy
       └─ JetUseDeployers Policy

JetUseDeployersグループの利用者
  └─ Deploy to Oracle Cloud（infra/orm）
       └─ JetUse専用コンパートメント内にアプリを構築

JetUseエンドユーザー
  └─ 作成されたOIDCユーザーでapp_urlへログイン
```

## 1. 管理者へ依頼する情報

管理者へ次の4点を伝える。

| 項目 | 記入例 |
|---|---|
| JetUse専用コンパートメント | `jetuse-sales` |
| 専用コンパートメントOCID | `<compartment_ocid>` |
| デプロイ担当グループ | `Default/JetUseDeployers` |
| IAM resource prefix | `jetuse-sales`（テナンシ内で一意） |

共有コンパートメントは使用しない。デプロイ担当グループは選択したコンパートメント内のリソースを作成・更新・削除でき、Resource Manager stateとjob outputも参照できる。stateには生成パスワードが含まれるため、グループメンバーを限定する。

## 2. IAM Bootstrap実行者の権限

最も簡単なのはテナンシのAdministratorsグループのメンバーがBootstrapを実行する方法である。委任する場合は、少なくとも次の権限が必要になる。

```text
Allow group <bootstrap-admin-group> to manage domains in tenancy
Allow group <bootstrap-admin-group> to manage policies in tenancy
Allow group <bootstrap-admin-group> to inspect compartments in tenancy
Allow group <bootstrap-admin-group> to manage orm-stacks in compartment id <compartment_ocid>
Allow group <bootstrap-admin-group> to manage orm-jobs in compartment id <compartment_ocid>
```

`manage domains` はDynamic Groupの作成に、`manage policies` はroot compartmentとJetUse専用コンパートメントへのPolicy作成に必要。Bootstrapはテナンシのホームリージョンで実行する。

## 3. デプロイ担当グループのPolicy

`infra/orm-bootstrap` は既存グループ（例: `Default/JetUseDeployers`）に次の6文を設定する。

```text
Allow group <deployer-group> to inspect compartments in tenancy
Allow group <deployer-group> to inspect tenancies in tenancy
Allow group <deployer-group> to read objectstorage-namespaces in tenancy
Allow group <deployer-group> to manage orm-stacks in compartment id <compartment_ocid>
Allow group <deployer-group> to manage orm-jobs in compartment id <compartment_ocid>
Allow group <deployer-group> to manage all-resources in compartment id <compartment_ocid>
```

最後の文はJetUse本体がVCN、ADB、Object Storage、Container Instances、Functions、API Gateway、Logging、Identity Domainを一括作成するために使用する。権限はJetUse専用コンパートメント内だけに限定され、テナンシIAMのDynamic GroupやPolicyは変更できない。

### デプロイ担当者に不要な権限

```text
# 付与しない
Allow group <deployer-group> to manage all-resources in tenancy
Allow group <deployer-group> to manage domains in tenancy
Allow group <deployer-group> to manage policies in tenancy
```

## 4. Dynamic Group

`<prefix>` はテナンシ内で一意にし、`<compartment_ocid>` はJetUse専用コンパートメントに置換する。

### 4.1 Runtime（必須）

名前: `<prefix>-runtime-dg`

```text
Any {all {resource.type='computecontainerinstance', resource.compartment.id='<compartment_ocid>'},
     all {resource.type='fnfunc', resource.compartment.id='<compartment_ocid>'}}
```

Container InstanceのFastAPIとFunctions routerは同じアプリ機能を実行するため、1つのRuntime Dynamic Groupにまとめる。

### 4.2 Autonomous Database（必須）

名前: `<prefix>-adb-dg`

```text
All {resource.type='autonomousdatabase', resource.compartment.id='<compartment_ocid>'}
```

ADBの `OCI$RESOURCE_PRINCIPAL` がSelect AI / DBMS_CLOUD_AIからGenerative AIとObject Storageへアクセスするために使用する。

### 4.3 Semantic Store（SQL Search使用時）

名前: `<prefix>-semantic-store-dg`

```text
All {resource.type='generativeaisemanticstore', resource.compartment.id='<compartment_ocid>'}
```

SQL Searchを使用しない場合は `enable_semantic_store=false` として省略できる。Semantic StoreはIAM反映後に作成する。

Bootstrapの作成範囲は次の2変数で分離できる。

- `enable_dynamic_group`: Runtime / ADB / Semantic StoreのDynamic Groupと、テナンシスコープのObject Storage namespace参照ポリシー
- `enable_runtime_policy`: JetUse専用コンパートメント内のRuntime Policy

コンパートメント管理者が後者だけを作成する場合は、テナンシ管理者が前者を先に適用し、同じ`prefix`を使用する。

## 5. Runtime Policy

### 5.1 Runtime / ADB / API Gateway（JetUse専用コンパートメント）

```text
Allow dynamic-group <prefix>-runtime-dg to use generative-ai-family in compartment id <compartment_ocid>
Allow dynamic-group <prefix>-runtime-dg to manage generative-ai-vectorstore in compartment id <compartment_ocid>
Allow dynamic-group <prefix>-runtime-dg to manage generative-ai-vectorstore-file in compartment id <compartment_ocid>
Allow dynamic-group <prefix>-runtime-dg to manage generative-ai-file in compartment id <compartment_ocid>
Allow dynamic-group <prefix>-runtime-dg to use autonomous-database-family in compartment id <compartment_ocid>
Allow dynamic-group <prefix>-runtime-dg to manage objects in compartment id <compartment_ocid>
Allow dynamic-group <prefix>-runtime-dg to read buckets in compartment id <compartment_ocid>
Allow dynamic-group <prefix>-runtime-dg to manage ai-service-speech-family in compartment id <compartment_ocid>
Allow dynamic-group <prefix>-runtime-dg to use ai-service-document-family in compartment id <compartment_ocid>
Allow dynamic-group <prefix>-runtime-dg to use ai-service-language-family in compartment id <compartment_ocid>
Allow dynamic-group <prefix>-runtime-dg to read tag-namespaces in compartment id <compartment_ocid>
Allow dynamic-group <prefix>-runtime-dg to use log-content in compartment id <compartment_ocid>
Allow dynamic-group <prefix>-runtime-dg to use metrics in compartment id <compartment_ocid>
Allow dynamic-group <prefix>-runtime-dg to read secret-family in compartment id <compartment_ocid>

Allow dynamic-group <prefix>-adb-dg to use generative-ai-family in compartment id <compartment_ocid>
Allow dynamic-group <prefix>-adb-dg to read objects in compartment id <compartment_ocid>

Allow any-user to use functions-family in compartment id <compartment_ocid> where ALL {request.principal.type = 'ApiGateway', request.resource.compartment.id = '<compartment_ocid>'}
```

### 5.2 Object Storage namespace（root compartment）

```text
Allow dynamic-group <prefix>-runtime-dg to read objectstorage-namespaces in tenancy
```

このroot-level Policyはnamespaceのread 1文だけで、Runtimeにテナンシ全体のObject Storage管理権限は与えない。

### 5.3 Semantic Store（有効時のみ）

```text
Allow dynamic-group <prefix>-semantic-store-dg to use database-tools-family in compartment id <compartment_ocid>
Allow dynamic-group <prefix>-semantic-store-dg to read secret-family in compartment id <compartment_ocid>
Allow dynamic-group <prefix>-semantic-store-dg to read database-family in compartment id <compartment_ocid>
Allow dynamic-group <prefix>-semantic-store-dg to read autonomous-database-family in compartment id <compartment_ocid>
Allow dynamic-group <prefix>-semantic-store-dg to use generative-ai-family in compartment id <compartment_ocid>
```

## 6. 機能と権限の対応

| JetUse機能 | Principal | 主なPolicy |
|---|---|---|
| Chat / Responses / Guardrails | Runtime | `use generative-ai-family` |
| RAG file upload / Vector Store | Runtime | `manage generative-ai-vectorstore*`, `manage generative-ai-file`, Object Storage |
| ADB wallet取得 / DB接続 | Runtime | `use autonomous-database-family`, Object Storage |
| Select AI / DBMS_CLOUD_AI | ADB | `use generative-ai-family`, `read objects` |
| 議事録 / STT / TTS | Runtime | `manage ai-service-speech-family`, objects, buckets, tag namespace |
| OCR | Runtime | `use ai-service-document-family` |
| OCI Language翻訳 | Runtime | `use ai-service-language-family` |
| OCI Logging / Monitoring | Runtime | `use log-content`, `use metrics` |
| Functions backend | API Gateway | 条件付き `use functions-family` |
| SQL Search enrichment | Semantic Store | DB Tools、Database metadata、Secret、GenAI |

## 7. 管理者への依頼文テンプレート

```text
JetUse Public版をOCI Resource Managerからデプロイするため、次の対応をお願いします。

1. JetUse専用コンパートメント: <name / OCID>
2. 既存デプロイ担当グループ: <domain/group>
3. IAM prefix: <prefix>
4. READMEのIAM Bootstrapボタンを、テナンシのホームリージョンで開いてPlan/Apply
5. Apply後、出力されたDynamic Group 2～3個とPolicy 3個を確認

通常デプロイ担当者にテナンシ管理権限は不要です。権限は上記専用コンパートメント内に限定してください。
```

## 8. 確認方法

管理者はBootstrap Apply後に次を確認する。

- Runtime DGのmatching ruleに `computecontainerinstance` と `fnfunc` がある。
- ADB DGが `autonomousdatabase` だけを対象にしている。
- SQL Search有効時だけSemantic Store DGがある。
- Deployer Policyの `manage all-resources` が `in compartment id ...` であり、`in tenancy` ではない。
- Runtime tenancy Policyが `read objectstorage-namespaces` の1文だけである。
- IAM反映を数分待ってから `infra/orm` のPlanを実行する。

自動テストと実OCI確認結果は [PUBLIC-IAM-01.md](../verification/PUBLIC-IAM-01.md) に記録する。

## 公式資料

- [Terraform Configurations for Resource Manager](https://docs.oracle.com/en-us/iaas/Content/ResourceManager/Concepts/terraformconfigresourcemanager.htm)
- [Resource Manager Policy Reference](https://docs.oracle.com/en-us/iaas/Content/Identity/policyreference/resourcemanagerpolicyreference.htm)
- [OCI Generative AI IAM Policies](https://docs.oracle.com/en-us/iaas/Content/generative-ai/iam-policies.htm)
- [Semantic Store Permissions](https://docs.oracle.com/en-us/iaas/Content/generative-ai/semantic-store-permissions.htm)
- [Autonomous Database Resource Principal](https://docs.oracle.com/en-us/iaas/autonomous-database-serverless/doc/resource-principal.html)
- [OCI Speech Policies](https://docs.oracle.com/en-us/iaas/Content/speech/using/policies.htm)
- [API Gateway to Functions Policy](https://docs.oracle.com/en-us/iaas/Content/APIGateway/Tasks/apigatewaycreatingpolicies.htm)
