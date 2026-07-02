# JetUse Public 版の IAM Bootstrap

> Public利用者・部門管理者向けの提出用チェックリストと全Policy一覧は
> [public-iam-requirements.md](./public-iam-requirements.md) を参照。
> 特定コンパートメントの`manage all-resources`だけを持つ利用者は
> [専用コンパートメント管理者向け手順](./public-deploy-dedicated-compartment.md)を参照。

Public 版では、テナンシ IAM の設定と JetUse 本体のデプロイを分離する。

1. テナンシ管理者が対象コンパートメントごとに `infra/orm-bootstrap` を一度だけ Apply
2. 通常のデプロイ担当者が GitHub の **Deploy to Oracle Cloud** から `infra/orm` を Apply
3. JetUse のエンドユーザーは OCI IAM 権限を必要とせず、作成された OIDC ユーザーでログイン

これにより、JetUse をデプロイする部門ユーザーへ Dynamic Group / Policy の管理権限を渡さずに済む。

## 管理者が準備するもの

- JetUse 専用コンパートメント。デプロイ担当者へ `all-resources` を許可するため、共有コンパートメントは使用しない。
- 既存の OCI IAM グループ（例: `Default/JetUseDeployers`）と、そのグループへの利用者追加。
- テナンシのホームリージョン名。Resource Manager が自動入力するのは `tenancy_ocid`、`compartment_ocid`、`region`、`current_user_ocid` のみで、ホームリージョンは含まれない。
- Bootstrap 実行者には Dynamic Group と Policy を作成できるテナンシ IAM 権限が必要。

Resource Manager の state と job 出力には生成パスワード等が含まれる。`JetUseDeployers` には対象スタックの state を参照できる人だけを所属させる。

## 推奨: Resource Manager で Bootstrap

GitHub の `main.zip` を構成ソースにしてスタックを作り、作業ディレクトリに `infra/orm-bootstrap` を指定する。

| 入力 | 例 | 説明 |
|---|---|---|
| `compartment_ocid` | JetUse 専用コンパートメント | Runtime とデプロイ権限の境界 |
| `home_region` | `us-ashburn-1` | OCI Console のテナンシ詳細で確認 |
| `prefix` | `jetuse-sales` | Dynamic Group 名が衝突しないテナンシ内で一意な値。アプリ本体と同じ値を推奨 |
| `enable_dynamic_group` | `true` | Dynamic Groupとテナンシスコープのnamespace参照ポリシーを作成 |
| `enable_runtime_policy` | `true` | JetUse専用コンパートメントのruntimeポリシーを作成 |
| `deployer_group_subject` | `Default/JetUseDeployers` | 既存グループ。`Allow group` より後の部分 |
| `enable_semantic_store` | `true` | SQL Search 不使用なら false |

`enable_dynamic_group` と `enable_runtime_policy` は独立して指定できる。

| Dynamic Group | Runtime Policy | 動作 |
|---|---|---|
| `true` | `true` | 管理者がすべてのruntime IAMを作成（標準） |
| `true` | `false` | Dynamic Groupとnamespace参照ポリシーだけを管理者が事前作成 |
| `false` | `true` | 既存Dynamic Groupを参照し、コンパートメント内のruntimeポリシーだけ作成 |
| `false` | `false` | runtime IAMをこのスタックでは作成しない |

`false / true`では、同じ`prefix`のDynamic Groupと`${prefix}-runtime-tenancy-policy`が
事前作成済みであること。`create_deployer_policy`はこの2フラグと独立している。
管理者用とコンパートメント管理者用は別のResource Manager Stack（別state）にする。
既にDynamic Groupを管理している同一stateで`enable_dynamic_group=false`へ変更すると、Terraformは
管理対象のDynamic Groupを削除する計画を立てるため、管理移管には先にstateの分離が必要となる。

Plan で次の IAM リソースだけが作成されることを確認してから Apply する。

- `${prefix}-runtime-dg`: Container Instances と Functions
- `${prefix}-adb-dg`: Autonomous Database の resource principal
- `${prefix}-semantic-store-dg`: Semantic Store（任意）
- `${prefix}-runtime-policy`: コンパートメント内の runtime 権限
- `${prefix}-runtime-tenancy-policy`: Object Storage namespace の read のみ
- `${prefix}-deployer-policy`: 通常デプロイ担当グループの権限

IAM の反映には数分かかることがある。Apply 完了後に `infra/orm` を実行する。

## デプロイ担当グループへ付与する権限

Bootstrap は次の Policy をテナンシ直下に作る。`<group>` と `<compartment_ocid>` を置換すれば、管理者が手動作成する場合にも使える。

```text
Allow group <group> to inspect compartments in tenancy
Allow group <group> to inspect tenancies in tenancy
Allow group <group> to read objectstorage-namespaces in tenancy
Allow group <group> to manage orm-stacks in compartment id <compartment_ocid>
Allow group <group> to manage orm-jobs in compartment id <compartment_ocid>
Allow group <group> to manage all-resources in compartment id <compartment_ocid>
```

`all-resources` は VCN、ADB、Object Storage、Container Instances、Functions、API Gateway、Logging、Identity Domain とその OIDC アプリを一つの専用コンパートメントに構築するために使用する。テナンシに対する `manage all-resources` や `manage domains in tenancy` は付与しない。

より細かい分離が必要な組織では、この文を各サービスの resource-family に分解できる。ただし JetUse の機能追加時に Policy も追随させる必要があるため、Public 版の標準は専用コンパートメント境界とする。

## Runtime Dynamic Group

権限の和集合を避けるため、アプリ実行基盤、ADB、Semantic Store を分離する。

### Container Instances / Functions

```text
Any {all {resource.type='computecontainerinstance', resource.compartment.id='<compartment_ocid>'},
     all {resource.type='fnfunc', resource.compartment.id='<compartment_ocid>'}}
```

主な権限は次のとおり。

- Generative AI の推論、Projects、Guardrails、hosted agent invocation
- Vector Store / Vector Store File / File の作成・削除
- ADB wallet の取得
- Object Storage の RAG 原本、音声、wallet の読み書き
- Speech、Document Understanding、Language
- Logging ingestion、Monitoring custom metrics
- 事前作成済み Vault secret の read（secret の作成権限は付与しない）

### Autonomous Database

```text
All {resource.type='autonomousdatabase', resource.compartment.id='<compartment_ocid>'}
```

ADB の `OCI$RESOURCE_PRINCIPAL` が Select AI / DBMS_CLOUD_AI から Generative AI と Object Storage を参照するために使用する。

### Semantic Store（任意）

```text
All {resource.type='generativeaisemanticstore', resource.compartment.id='<compartment_ocid>'}
```

Database Tools、secret、Database / Autonomous Database metadata、Generative AI inference の read/use を付与する。Semantic Store は IAM の反映後に作成する。IAM より先に作ると enrichment が失敗したままになり、再作成が必要になった実測がある。

実際の Policy 文の正本は [../../infra/terraform/modules/iam/main.tf](../../infra/terraform/modules/iam/main.tf)。手作業で転記せず、可能な限り Bootstrap を使用する。

## オプション機能

### 認証付き MCP サーバーの登録

現状の Public 標準は事前作成済み secret の read のみ。アプリ自身に secret を作成させる場合は、リスクを確認した上で runtime Dynamic Group に次を追加する。

```text
Allow dynamic-group jetuse-runtime-dg to manage secret-family in compartment id <compartment_ocid>
```

### Hosted Application / Deployment の作成

Public ORM は hosted deployment 自体を作成しない。別途作成する場合は hosted application / deployment 専用 Dynamic Group と、OCIR repository の read を追加する。過去の実測では Dynamic Group 反映に 5〜10 分かかる場合がある。手順は [hosted-agent-oauth.md](./hosted-agent-oauth.md) を参照。

## トラブルシュート

| 症状 | 確認点 |
|---|---|
| IAM 作成が 403 / home region エラー | Bootstrap の `home_region` がテナンシのホームリージョンか |
| 通常利用者が Stack / Apply を作れない | グループ所属と `${prefix}-deployer-policy`、スタックのコンパートメント |
| Chat / RAG が 404 NotAuthorizedOrNotFound | runtime DG の matching rule、IAM 反映待ち、対象コンパートメント |
| 議事録 job が INTERNAL_ERROR | Speech、objects、buckets、tag-namespaces の Policy |
| API Gateway の Functions route が 500 | `request.principal.type = 'ApiGateway'` の functions-family Policy |
| SQL Search enrichment が失敗 | Semantic Store が IAM より後に作成されたか |

## 参考

- [Resource Manager の Terraform 変数](https://docs.oracle.com/en-us/iaas/Content/ResourceManager/Concepts/terraformconfigresourcemanager.htm)
- [Resource Manager Policy Reference](https://docs.oracle.com/en-us/iaas/Content/Identity/policyreference/resourcemanagerpolicyreference.htm)
- [OCI Generative AI IAM Policies](https://docs.oracle.com/en-us/iaas/Content/generative-ai/iam-policies.htm)
- [Semantic Store Permissions](https://docs.oracle.com/en-us/iaas/Content/generative-ai/semantic-store-permissions.htm)
- [OCI Speech Policies](https://docs.oracle.com/en-us/iaas/Content/speech/using/policies.htm)
