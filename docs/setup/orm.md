# OCI Resource ManagerでJetUse Public版をデプロイ

GitHubの**Deploy JetUse to Oracle Cloud**ボタンから、IAMとJetUse本体を1つのOCI Resource Manager（ORM）Stackとして構築する。

[![Deploy JetUse to Oracle Cloud](https://oci-resourcemanager-plugin.plugins.oci.oraclecloud.com/latest/deploy-to-oracle-cloud.svg)](https://cloud.oracle.com/resourcemanager/stacks/create?zipUrl=https://github.com/sogawa-yk/jetuse/releases/download/orm-main/jetuse-orm.zip)

専用ZIPの直下にTerraformと`schema.yaml`があるため、Working directoryの指定は不要。

## IAM作成範囲

同じStackの変数画面で、実行ユーザーの権限と既存IAMに合わせて選択する。

| 実行条件 | `enable_dynamic_group` | `enable_runtime_policy` | 動作 |
|---|---:|---:|---|
| テナンシIAM管理者 | `true` | `true` | Dynamic Group、テナンシPolicy、コンパートメントPolicyを作成 |
| Dynamic Group作成済み | `false` | `true` | 既存Dynamic Groupを参照してコンパートメントPolicyだけ作成 |
| Runtime IAM作成済み | `false` | `false` | IAMを変更せずアプリリソースだけ作成 |
| Dynamic Groupだけ作成 | `true` | `false` | Dynamic GroupとテナンシPolicyだけ作成 |

`enable_semantic_store=false`にすると、SQL Search用Semantic StoreのDynamic GroupとPolicy文を作成しない。

Terraformは権限を迂回しない。実行ユーザーに権限がないIAM操作を有効にした場合、そのリソースのPlanまたはApplyがOCIの`403`で失敗する。権限の詳細は [IAMガイド](./iam.md) を参照。

## 作成手順

1. READMEの**Deploy JetUse to Oracle Cloud**ボタンを開く。
2. Stack compartmentとリソース作成先にJetUse専用コンパートメントを選ぶ。
3. IAM作成範囲を上表から選ぶ。新規テナンシの管理者は既定値のままでよい。
4. `prefix`をテナンシ内で一意にする。`enable_dynamic_group=false`にした場合は既存のDynamic Group名を`existing_dynamic_group`に入力する。
5. Planで作成先、IAM、課金対象を確認してApplyする。

Resource Managerが自動入力する`region`はリソースの配備リージョンであり、テナンシのホームリージョンではない。Identity DomainとIAMのCREATEに必要なホームリージョンは、Stackがregion subscriptionsから自動導出する（ユーザー入力不要）。

## 主な入力

| 入力 | 既定 | 説明 |
|---|---:|---|
| `compartment_ocid` | 必須 | JetUseリソースの作成先 |
| `prefix` | `jetuse` | リソース、Dynamic Group、Policy名のprefix |
| `enable_dynamic_group` | `true` | Dynamic Groupとnamespace参照Policyを作成 |
| `existing_dynamic_group` | 空 | `enable_dynamic_group=false`時に全Policy文が参照する既存Dynamic Group名（必須） |
| `enable_runtime_policy` | `true` | 対象コンパートメントにRuntime Policyを作成 |
| `enable_semantic_store` | `true` | SQL Search用Semantic Store権限を含める |
| `enable_auth` | `true` | Identity Domain、OIDCアプリ、デモユーザーを作成 |
| `enable_opensearch` | `false` | 常設課金のOpenSearchを作成 |
| `adb_admin_password` | 空 | 空の場合は安全なランダム値を生成 |

`enable_auth=true`はIdentity Domainを作成するため、実行ユーザーにテナンシのDomain管理権限が必要。権限がなく認証も不要な隔離検証環境では`false`にできる。

## 作成されるリソース

- 選択に応じたDynamic GroupとIAM Policy
- VCN、public/private subnet、NSG、Internet/NAT/Service Gateway
- Autonomous Database 26aiとwallet
- Object Storage（SPA、app-data、speech）
- Container Instance、OCI Functions、API Gateway
- Logging / Monitoring
- Identity Domain、OIDC public client、初期デモユーザー（`enable_auth=true`）
- OpenSearch cluster（`enable_opensearch=true`）

## デプロイ後

1. Outputの`app_url`を開く。
2. `demo_username` / `demo_password`でログインする。
3. 初回はIAM反映、ADB作成、DB初期化に10〜15分程度かかる。反映中は一部APIが一時的に失敗することがある。

## Stack更新時の注意

同じStackで`enable_dynamic_group=true`から`false`へ変更すると、TerraformはそのStackが管理しているDynamic GroupとテナンシPolicyを削除するPlanを作る。既存IAMへ管理を移す場合は、Planを確認し、必要に応じて先にTerraform stateを移管する。

StackをDestroyすると、そのStackで作成したIAMも削除対象になる。共有IAMをこのStackに作らせない場合は、初回から該当フラグを`false`にする。

## 配布と検証

`.github/workflows/release.yml`が`main`から`jetuse-orm.zip`を生成し、`orm-main`リリースへ公開する。CIはZIPを展開し、ルートの`schema.yaml`にIAM変数があることと、展開したTerraformが`validate`できることを確認する。

ローカルでは次を実行する。

```bash
terraform -chdir=infra/orm init -backend=false
terraform -chdir=infra/orm validate
bash scripts/package-orm-stacks.sh /tmp/jetuse-orm
```
