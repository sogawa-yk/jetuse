# JetUse Public版のIAM設定

Public版はIAMとアプリ本体を1つの`infra/orm` Stackで管理する。別のBootstrap Stackは使用しない。

TerraformコードにIAMリソースが含まれていても、実行ユーザーのOCI権限を超えて作成されることはない。Resource Managerの変数で作成範囲を選択し、権限のない操作を有効にした場合はPlanまたはApplyが権限エラーになる。

## 作成範囲を選ぶ変数

| 変数 | 既定 | 作成物 |
|---|---:|---|
| `enable_dynamic_group` | `true` | Runtime / ADB / Semantic StoreのDynamic Groupと、テナンシスコープのObject Storage namespace参照Policy |
| `enable_runtime_policy` | `true` | JetUse専用コンパートメント内のRuntime Policy |
| `enable_semantic_store` | `true` | SQL Search用Semantic Store Dynamic GroupとPolicy文 |

`enable_dynamic_group`と`enable_runtime_policy`は独立して指定できる。

| Dynamic Group | Runtime Policy | 使用例 |
|---:|---:|---|
| `true` | `true` | 新規テナンシで管理者がすべて作成 |
| `true` | `false` | テナンシ管理者がDynamic Groupだけ作成 |
| `false` | `true` | コンパートメント管理者が既存Dynamic Group向けPolicyだけ作成 |
| `false` | `false` | IAMが作成済みでアプリだけ作成 |

`false / true`では、同じ`prefix`のDynamic Groupと`${prefix}-runtime-tenancy-policy`が作成済みであることが前提。

## 必要な実行ユーザー権限

### すべて作成する場合

最も簡単なのはテナンシのAdministratorsグループに所属するユーザーが実行する方法。委任する場合は、少なくとも次の操作を許可する。

- Dynamic Groupの作成・更新・削除
- root compartmentの`${prefix}-runtime-tenancy-policy`の管理
- JetUse専用コンパートメントの`${prefix}-runtime-policy`の管理
- JetUseアプリリソースの管理
- `enable_auth=true`の場合はIdentity Domainの管理
- Resource Manager Stack / Jobの管理

組織のIAM設計に合わせて個別Policyを作る場合は [Public版IAM要件](./public-iam-requirements.md) のPolicy一覧を使用する。

### コンパートメントの`manage all-resources`だけを持つ場合

テナンシ全体のDynamic Groupやroot compartmentのPolicyは作成できないため、通常は次の値を使う。

```text
enable_dynamic_group  = false
enable_runtime_policy = true   # 対象コンパートメントでPolicyを管理できる場合
```

Runtime Policyも事前作成済みなら両方`false`にする。`enable_auth=true`でIdentity Domainを新規作成するには、別途Domain管理権限が必要。

## 作成されるIAM

| リソース | 目的 |
|---|---|
| `${prefix}-runtime-dg` | Container Instances / Functionsのresource principal |
| `${prefix}-adb-dg` | Autonomous Databaseのresource principal |
| `${prefix}-semantic-store-dg` | SQL Search Semantic Store（任意） |
| `${prefix}-runtime-policy` | JetUse実行時権限。JetUse専用コンパートメント内 |
| `${prefix}-runtime-tenancy-policy` | Object Storage namespaceのread。root compartment |

Runtime PolicyにはGenerative AI、Vector Store、ADB、Object Storage、Speech、Document、Language、Logging、Monitoring、Secrets、API GatewayからFunctionsへの呼び出し権限が含まれる。Policy文の正本は [IAM Terraform module](../../infra/terraform/modules/iam/main.tf)。

## 既存IAMを使う場合

Dynamic Group名は`prefix`から決まるため、既存名とStackの`prefix`を一致させる。

```text
<prefix>-runtime-dg
<prefix>-adb-dg
<prefix>-semantic-store-dg
```

SQL Searchを使用しない場合は`enable_semantic_store=false`にする。

## StateとDestroy

フラグを`true`にして作成したIAMはアプリと同じTerraform stateで管理される。StackのDestroyではIAMも削除対象になる。

同じStackでフラグを`true`から`false`へ変更すると、対象IAMの削除Planになる。単に「今後管理しない」という意味にはならないため、既存IAMへ移管する場合はstate移管を先に行う。

## トラブルシュート

| 症状 | 主な原因 |
|---|---|
| Dynamic Group作成が403 | `enable_dynamic_group=true`だがテナンシIAM権限がない |
| root compartmentのPolicy作成が403 | namespace参照Policyを作る権限がない |
| Runtime Policy作成が403 | 対象コンパートメントのPolicy管理権限がない |
| Identity Domain作成が403 | `enable_auth=true`だがDomain管理権限がない |
| Apply後にChat/RAGが403 | Dynamic Group / Runtime Policy不足、prefix不一致、またはIAM反映待ち |
| `false / true`でPolicyが無効 | 参照する既存Dynamic Groupが存在しない |

IAM反映には数分かかることがある。Apply完了直後にresource principalが認可されない場合は、Dynamic GroupのMatching RuleとPolicyを確認してから5〜10分待つ。
