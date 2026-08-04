# JetUse Public版 IAM要件

JetUse Public版は、IAMとアプリ本体を1つのOCI Resource Manager Stackからデプロイする。実行ユーザーの権限と既存IAMに合わせて、Stack内のIAM作成範囲を選択する。

Terraform実装の詳細は [IAMガイド](./iam.md)、操作手順は [Resource Managerガイド](./orm.md) を参照する。

## 役割

| 役割 | OCI IAMユーザー | 必要な権限 |
|---|---:|---|
| JetUseエンドユーザー | 不要 | 作成されたOIDCユーザーだけ |
| テナンシ管理者としてDeploy | 必要 | Dynamic Group / Policy / Domain / ORM / アプリリソース管理 |
| 専用コンパートメント管理者としてDeploy | 必要 | 専用コンパートメントの`manage all-resources`。Dynamic Groupとroot Policyだけ事前作成 |
| Container Instance / Functions / ADB / ホスト型エージェント | Resource Principal | Stackまたは管理者が作成したRuntime Policy |

## 権限別のStack設定

| 実行ユーザー | `enable_dynamic_group` | `enable_runtime_policy` | 事前作業 |
|---|---:|---:|---|
| テナンシIAM管理者 | `true` | `true` | なし |
| 専用コンパートメントの`manage all-resources`のみ | `false` | `true` | Dynamic Group 1本 + デプロイ担当グループへの`inspect tenancies in tenancy` |
| コンパートメント内のPolicy作成も許されない | `false` | `false` | 上記 + Runtime Policy 全文（本書「Runtime Policy」節） |

**`enable_auth=true`（Identity Domain の作成とドメイン内のユーザー・OIDCアプリ管理）は、
専用コンパートメントの `manage all-resources` で実行できる**。テナンシ側のDomain権限は不要
（2026-07-30 実機検証。`docs/verification/PUBLIC-IAM-02.md`）。

専用コンパートメント権限だけでデプロイする手順の正本は
[専用コンパートメント管理者向けガイド](./public-deploy-dedicated-compartment.md)。

## Resource Manager実行ユーザー

JetUse専用コンパートメントに限定して、次の権限を付与する。

```text
Allow group <deployer-group> to inspect tenancies in tenancy
Allow group <deployer-group> to inspect compartments in tenancy
Allow group <deployer-group> to manage orm-stacks in compartment id <compartment_ocid>
Allow group <deployer-group> to manage orm-jobs in compartment id <compartment_ocid>
Allow group <deployer-group> to manage all-resources in compartment id <compartment_ocid>
```

テナンシスコープで**必須なのは`inspect tenancies in tenancy`の1文だけ**（実測。これが無いと
リージョン購読一覧を読めず、planがリージョン解決で失敗する）。`inspect compartments in tenancy`は
コンソールでコンパートメントを選択・表示するために付ける。
`read objectstorage-namespaces in tenancy`は不要（上記「Runtime Policy」節の注記を参照）。

この権限ではDynamic Groupを作成できないため事前作成が要る。一方、**Identity Domainと
コンパートメント内のPolicyは`manage all-resources in compartment`で作成できる**（実測）ので、
`enable_auth=false`にしたり Runtime Policy を事前作成したりする必要はない。

通常のデプロイ担当者へ`manage all-resources in tenancy`を付与しない。

## Dynamic Group

`enable_dynamic_group=true`の場合、Stackが責務別の3つのDynamic Group（`<prefix>-runtime-dg` / `<prefix>-adb-dg` / `<prefix>-semantic-store-dg`）を作成する。

`enable_dynamic_group=false`の場合は、**単一の**Dynamic Groupを事前作成し、その名前をStack変数`existing_dynamic_group`に入力する。名前は任意。Matching RuleにJetUseの全runtimeプリンシパルを含める。

```text
Any {all {resource.type='computecontainerinstance', resource.compartment.id='<compartment_ocid>'},
     all {resource.type='fnfunc', resource.compartment.id='<compartment_ocid>'},
     all {resource.type='autonomousdatabase', resource.compartment.id='<compartment_ocid>'},
     all {resource.type='generativeaisemanticstore', resource.compartment.id='<compartment_ocid>'},
     all {resource.type='generativeaihostedapplication', resource.compartment.id='<compartment_ocid>'},
     all {resource.type='generativeaihostedapplicationiam', resource.compartment.id='<compartment_ocid>'},
     all {resource.type='generativeaihosteddeployment', resource.compartment.id='<compartment_ocid>'}}
```

- SQL Searchを使用しない場合は`enable_semantic_store=false`にし、`generativeaisemanticstore`の行を省略できる。
- ホスト型エージェント（PORT-03 / ADR-0019）を使わない場合は`enable_hosted_agents=false`にし、
  `generativeaihosted*`の3行を省略できる。使う場合は**3行すべて**必要で、
  `generativeaihostedapplicationiam`を落とすと配備は進むのに実行時のresource principalがDGに入らない。
  さらにStack変数`existing_iam_covers_hosted_agents=true`が必要（未設定だとplanで停止する）。

## Runtime Policy

JetUse専用コンパートメントの`${prefix}-runtime-policy`には次の権限が含まれる。

- Runtime: Generative AI、Response / Conversation、Vector Store / File / Project、ADB、Object Storage、Speech、Document、Language、Logging、Monitoring、Secrets
- ADB: Generative AI、Object Storage read
- API Gateway: 同じコンパートメントのFunctions呼び出し
- Semantic Store: DB Tools、Database metadata、Secrets、Generative AI（有効時）

root compartmentの`${prefix}-runtime-tenancy-policy`は次の1文だけを持つ。

```text
Allow dynamic-group <prefix>-runtime-dg to read objectstorage-namespaces in tenancy
```

**この1文は実測では不要**（2026-07-30 / PUBLIC-IAM-02）。`manage all-resources in compartment`相当を持つ
プリンシパルは、この権限を一度も付与されていなくても`GetNamespace`を呼べる。削除した状態でRAGの
アップロードと議事録ジョブが最後まで成功することも確認した。したがって
`enable_dynamic_group=false`（IAM事前作成）の運用で、**このPolicyを事前作成する必要はない**。
スタックがIAMを作る構成では引き続き作成するが、保険の位置づけである。

完全なPolicy文の正本は [IAM Terraform module](../../infra/terraform/modules/iam/main.tf)。

agentic API（Responses / Conversations / Vector Store / Files / Project）は`generative-ai-family`に**含まれない**個別resource-typeである。Runtime Policyを事前作成する場合は、次の6つを漏らさないこと（欠けると既定チャットモデル・RAG回答・会話メモリがresource principalでのみ404になる）。

```text
Allow dynamic-group <prefix>-runtime-dg to manage generative-ai-response in compartment id <compartment_ocid>
Allow dynamic-group <prefix>-runtime-dg to manage generative-ai-conversation in compartment id <compartment_ocid>
Allow dynamic-group <prefix>-runtime-dg to manage generative-ai-vector-store in compartment id <compartment_ocid>
Allow dynamic-group <prefix>-runtime-dg to manage generative-ai-vectorstore-file in compartment id <compartment_ocid>
Allow dynamic-group <prefix>-runtime-dg to manage generative-ai-file in compartment id <compartment_ocid>
Allow dynamic-group <prefix>-runtime-dg to manage generative-ai-project in compartment id <compartment_ocid>
```

## 管理者への依頼テンプレート

```text
JetUse Public版をOCI Resource Managerからデプロイします。

1. JetUse専用コンパートメント: <name / OCID>
2. デプロイ担当グループ: <domain/group>
3. IAM prefix: <prefix>
4. 実行ユーザーがDynamic Groupを作成できない場合:
   JetUse用のDynamic Group（単一。Matching Ruleは本書のDynamic Group節）を事前作成し、
   その名前を教えてください。あわせてデプロイ担当グループへ
   `Allow group <deployer-group> to inspect tenancies in tenancy` を付与してください
   （この1文が無いとplanがリージョン解決で失敗します）。
   namespace参照Policyの事前作成は不要です（実測。本書「Runtime Policy」節の注記）。
5. 実行ユーザーがコンパートメントPolicyを作成できない場合:
   <prefix>-runtime-policyも事前作成してください。

事前作成された範囲に応じて、Resource Manager画面の
enable_dynamic_group / enable_runtime_policyをfalseにします。
```

## 確認項目

- Dynamic GroupのMatching Ruleが対象コンパートメントだけを指している。
- Runtime Policyの各文がJetUse専用コンパートメントに限定されている。
- デプロイ担当グループが`inspect tenancies in tenancy`を持っている（分割IAM運用で必須）。
- Stack変数`existing_dynamic_group`が既存Dynamic Group名と一致している。
- IAM反映後5〜10分待ってからresource principalの動作を確認する。

## 公式資料

- [Resource Manager Policy Reference](https://docs.oracle.com/en-us/iaas/Content/Identity/policyreference/resourcemanagerpolicyreference.htm)
- [OCI Generative AI IAM Policies](https://docs.oracle.com/en-us/iaas/Content/generative-ai/iam-policies.htm)
- [Semantic Store Permissions](https://docs.oracle.com/en-us/iaas/Content/generative-ai/semantic-store-permissions.htm)
- [Autonomous Database Resource Principal](https://docs.oracle.com/en-us/iaas/autonomous-database-serverless/doc/resource-principal.html)
