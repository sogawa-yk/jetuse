# JetUse Public版デプロイ手順: 専用コンパートメント管理者

このガイドは、テナンシ管理権限を持たず、JetUse専用コンパートメントに対して
`manage all-resources`を付与されている利用者向けである。

```text
Allow group <deployer-group> to manage all-resources in compartment id <compartment_ocid>
```

Dynamic GroupとテナンシスコープのPolicyはテナンシ管理者が事前作成する。
コンパートメント管理者は、既存Dynamic Groupを参照するRuntime PolicyとJetUse本体を
自分のコンパートメント内にデプロイする。

## 1. 全体の流れ

```text
テナンシ管理者（1回）
  └─ 管理者用IAM Stack
       enable_dynamic_group  = true
       enable_runtime_policy = false
       ├─ Runtime / ADB / Semantic Store Dynamic Group
       └─ Object Storage namespace参照Policy（テナンシスコープ）

コンパートメント管理者（1回）
  └─ Runtime Policy Stack
       enable_dynamic_group  = false
       enable_runtime_policy = true
       └─ JetUse専用コンパートメントのRuntime Policy

コンパートメント管理者
  └─ JetUse Application Stack（infra/orm）
       └─ VCN / ADB / API Gateway / Container Instances / Functions等
```

管理者用IAM Stack、Runtime Policy Stack、JetUse Application Stackは、それぞれ別の
Resource Manager Stack（別state）として作成する。同じStackでフラグを切り替えて管理を移管しない。

## 2. 対象者チェック

すべて該当する場合にこのガイドを使用する。

- JetUse専用コンパートメントが割り当てられている。
- 専用コンパートメントで`manage all-resources`を持っている。
- テナンシのDynamic Groupやroot compartmentのPolicyを変更できない。
- GitHubのDeploy to Oracle CloudからOCI Resource Managerを利用する。

テナンシ管理権限を持つ場合は、[テナンシ管理者向けガイド](./public-deploy-tenancy-admin.md)を使用する。

## 3. `manage all-resources`で可能なこと／できないこと

| 操作 | 実行可否 | 備考 |
|---|---:|---|
| Resource Manager Stack / Job | 可 | 対象コンパートメント内 |
| VCN / ADB / Bucket / Functions等 | 可 | 対象コンパートメント内 |
| Identity Domain / OIDCアプリ | 可 | 対象コンパートメント内 |
| Runtime Policy | 可 | Policyの配置先が対象コンパートメントのため |
| Dynamic Group | 不可 | テナンシ／Default Identity Domainの資源 |
| Object Storage namespace参照Policy | 不可 | root compartmentに配置するPolicy |
| 他コンパートメントの資源 | 不可 | 権限境界外 |

`manage all-resources in compartment`は、コンパートメント内のリソース管理権限である。
テナンシIAMやコンパートメント自体の作成・削除権限は含まない。

## 4. テナンシ管理者へ渡す情報

次の値を管理者と合意する。特に`prefix`と`enable_semantic_store`は、後続のRuntime Policy
Stackでも同じ値を使用する。

| 項目 | 記入例 |
|---|---|
| 専用コンパートメント名 | `jetuse-sales` |
| 専用コンパートメントOCID | `<compartment_ocid>` |
| デプロイ担当グループ | `Default/JetUseDeployers` |
| IAM prefix | `jetuse-sales` |
| テナンシのホームリージョン | `us-ashburn-1` |
| 配備リージョン | `ap-osaka-1` |
| SQL Search / Semantic Store | 使用する／使用しない |

### 管理者に依頼するBootstrap

テナンシ管理者は`infra/orm-bootstrap`を次の値で、テナンシのホームリージョンからApplyする。

| 変数 | 値 |
|---|---|
| `compartment_ocid` | JetUse専用コンパートメントOCID |
| `prefix` | 合意したIAM prefix |
| `enable_dynamic_group` | `true` |
| `enable_runtime_policy` | `false` |
| `enable_semantic_store` | SQL Searchを使用する場合`true` |
| `create_deployer_policy` | 既に権限付与済みなら`false` |

このStackが作成する資源は次のとおり。

- `<prefix>-runtime-dg`
- `<prefix>-adb-dg`
- `<prefix>-semantic-store-dg`（`enable_semantic_store=true`の場合）
- `<prefix>-runtime-tenancy-policy`

最後のPolicyは、Runtime Dynamic Groupに`read objectstorage-namespaces in tenancy`だけを付与する。

## 5. 追加で必要なテナンシ参照権限

`manage all-resources in compartment`だけでは、Resource Manager画面のコンパートメント選択や
Object Storage namespace取得に必要なテナンシ参照権限が不足する場合がある。管理者に次の3文を確認する。

```text
Allow group <deployer-group> to inspect compartments in tenancy
Allow group <deployer-group> to inspect tenancies in tenancy
Allow group <deployer-group> to read objectstorage-namespaces in tenancy
```

組織がResource Manager権限を個別管理している場合は、次も明示する。

```text
Allow group <deployer-group> to manage orm-stacks in compartment id <compartment_ocid>
Allow group <deployer-group> to manage orm-jobs in compartment id <compartment_ocid>
```

`manage all-resources`にResource Managerが含まれる場合、後者2文は権限上は重複する。

次のテナンシ管理権限は付与しない。

```text
# 不要・付与しない
Allow group <deployer-group> to manage all-resources in tenancy
Allow group <deployer-group> to manage domains in tenancy
Allow group <deployer-group> to manage policies in tenancy
Allow group <deployer-group> to manage dynamic-groups in tenancy
```

## 6. コンパートメント管理者によるRuntime Policy作成

管理者用Bootstrapの完了後、IAM反映を5～10分待つ。続いて、コンパートメント管理者が
別のResource Manager Stackを作成する。

1. GitHubの`main.zip`をStackの構成ソースに指定する。
2. Working directoryに`infra/orm-bootstrap`を指定する。
3. Stack compartmentにJetUse専用コンパートメントを指定する。
4. 次の変数を設定する。

| 変数 | 値 |
|---|---|
| `compartment_ocid` | JetUse専用コンパートメントOCID |
| `home_region` | テナンシのホームリージョン |
| `prefix` | 管理者用Bootstrapと同じ値 |
| `enable_dynamic_group` | `false` |
| `enable_runtime_policy` | `true` |
| `enable_semantic_store` | 管理者用Bootstrapと同じ値 |
| `create_deployer_policy` | `false` |

5. Planを実行する。
6. Planに次の1資源だけが含まれることを確認する。

```text
module.iam.oci_identity_policy.runtime[0]
```

次の資源がPlanに含まれている場合はApplyしない。

```text
oci_identity_dynamic_group.*
oci_identity_policy.runtime_tenancy[*]
oci_identity_policy.deployer[*]
```

7. Applyを実行する。
8. `<prefix>-runtime-policy`がJetUse専用コンパートメントに作成されたことを確認する。

### よくある失敗

- Dynamic Groupが見つからない: `prefix`または`enable_semantic_store`が管理者用Bootstrapと不一致。
- Policy作成が403: `manage all-resources`の対象コンパートメントが異なる、または権限反映待ち。
- root compartmentへのPolicy作成がPlanされる: `enable_dynamic_group`または`create_deployer_policy`が誤って`true`。

## 7. JetUse本体のデプロイ

Runtime Policyの反映を5～10分待ってから、JetUse Application Stackを作成する。

1. READMEの**Deploy to Oracle Cloud**ボタンを開く。
2. Stack compartmentにJetUse専用コンパートメントを指定する。
3. Working directoryに`infra/orm`を指定する。
4. `compartment_ocid`に同じ専用コンパートメントを指定する。
5. `home_region`にテナンシのホームリージョンを指定する。
6. `prefix`にIAM Bootstrapと同じ値を指定する。
7. 必要に応じてオプションを設定する。

| 変数 | 推奨値／注意 |
|---|---|
| `enable_auth` | Public利用では`true`を推奨 |
| `enable_opensearch` | 高コストのため必要時のみ`true` |
| `rate_limit_rps` | Public公開では`0`にしない |
| `api_image_url` / `fn_router_image` | 通常は既定値。組織指定イメージがある場合だけ変更 |

8. Planを実行する。
9. 作成先がJetUse専用コンパートメント内であることを確認する。
10. PlanにDynamic GroupやIAM Policyが含まれないことを確認する。
11. Applyを実行する。

## 8. デプロイ後の確認

1. Outputの`app_url`を開く。
2. `demo_username` / `demo_password`でログインする。
3. Chatで短いメッセージを送信する。
4. RAGへ小さいテキストファイルを登録し、処理完了を確認する。
5. DB画面を開き、ADB接続を確認する。
6. Functions経由のAPI（presets、dbchat、tts等）を確認する。
7. 利用予定に応じてSpeech、OCR、翻訳、SQL Searchを確認する。

Resource Managerのstateとjob outputには生成パスワードが含まれる。Stackを閲覧できるユーザーを限定し、
stateやパスワードを問い合わせチケットへ添付しない。

## 9. 権限エラーの切り分け

| 症状 | 主な原因 | 対応 |
|---|---|---|
| Stackを作成できない | ORM権限または`inspect compartments`不足 | テナンシ管理者へ追加権限を依頼 |
| Object Storage namespace取得失敗 | `read objectstorage-namespaces`不足 | テナンシ管理者へ依頼 |
| Runtime Policy作成が403 | 対象コンパートメントの権限不足 | Policyと選択コンパートメントを確認 |
| Runtime PolicyでDynamic Group参照エラー | prefix不一致／管理者Bootstrap未完了 | 管理者側のDG名を確認 |
| Application Applyで一部だけ403/404 | `manage all-resources`の対象違い | 失敗resourceとcompartment OCIDを確認 |
| Apply後にChat/RAG/OCRが404 | Runtime Policy不足またはIAM反映待ち | Policy文と反映時間を確認 |
| Select AIが認可エラー | ADB Dynamic GroupまたはPolicy不足 | `<prefix>-adb-dg`を確認 |
| Functions経由APIが500 | API Gateway→Functions文が不足 | Runtime Policyを確認 |
| Identity Domainがhome regionエラー | `home_region`が誤り | テナンシ詳細のHome regionを確認 |

問い合わせ時は、次だけを共有する。

- Stack OCID / Job OCID
- 失敗したTerraform resource名
- OCIエラーコードとrequest ID
- 利用したcompartment名とIAM prefix

Terraform state、生成パスワード、秘密鍵、Auth Tokenは共有しない。

## 10. 更新・再デプロイ・削除

- アプリ更新: JetUse Application StackでPlan / Applyする。
- Runtime Policy更新: コンパートメント管理者のRuntime Policy StackでPlan / Applyする。
- アプリ削除: JetUse Application StackをDestroyする。
- 再デプロイ予定がある場合、Runtime Policy Stackは残してよい。
- Runtime Policy StackをDestroyしても、管理者用Dynamic Groupとnamespace参照Policyは削除されない。
- Dynamic GroupとテナンシスコープPolicyの削除はテナンシ管理者へ依頼する。

削除時は、JetUse Application Stackを先にDestroyし、その後Runtime Policy Stackを削除する。

## 関連資料

- [Public版 IAM Bootstrap](./iam.md)
- [Public版 IAM要件](./public-iam-requirements.md)
- [テナンシ管理者向けガイド](./public-deploy-tenancy-admin.md)
- [Resource Managerデプロイ](./orm.md)
- [Dynamic Group構成](./dynamic-group-matching-rules.md)
