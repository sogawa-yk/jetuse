# JetUse Public版デプロイガイド: 専用コンパートメント管理者

このガイドは、テナンシ管理権限を持たず、JetUse専用コンパートメントに対して次の権限を持つ利用者向けである。

```text
Allow group <deployer-group> to manage all-resources in compartment id <compartment_ocid>
```

利用者は1つのJetUse Stackを実行する。Dynamic Groupなど権限のないIAMは管理者が事前設定し、Resource Manager画面で該当する作成フラグを`false`にする。

## 対象者チェック

すべてYesの場合にこのガイドを使用する。

- JetUse専用コンパートメントを割り当てられている。
- 専用コンパートメントで`manage all-resources`を持っている。
- テナンシの`manage domains`または`manage policies`を持っていない。
- GitHubのDeploy to Oracle CloudボタンからResource Managerを利用する。

テナンシ管理権限を持つ場合は [テナンシ管理者向けガイド](./public-deploy-tenancy-admin.md) を使用する。

## 権限の分担

| 作業 | 専用コンパートメント利用者 | テナンシ管理者 |
|---|---:|---:|
| Resource Manager Stack / Job | 実行 | 事前権限を付与 |
| VCN / ADB / Bucket / Functions等 | 作成・更新・削除 | 原則不要 |
| Dynamic Group | 実行不可 | 事前作成 |
| Runtime Policy | コンパートメント内で許可されていれば作成 | 必要に応じて事前作成 |
| JetUseアプリの利用 | OIDCで利用 | 不要 |

## 1. テナンシ管理者へ依頼する作業

次の情報を管理者へ渡す。

| 項目 | 内容 |
|---|---|
| 専用コンパートメント名 | `<compartment_name>` |
| 専用コンパートメントOCID | `<compartment_ocid>` |
| デプロイ担当グループ | `<identity-domain>/<group-name>` |
| 配備リージョン | 例: `ap-osaka-1` |
| SQL Search | 使用する / 使用しない |

管理者へ次を依頼する。

1. 専用コンパートメントを対象にするDynamic Groupを作成する。
2. Dynamic GroupへJetUse Runtime Policyを設定する。
3. デプロイ担当グループへ不足するResource Manager / tenancy read権限を設定する。
4. テナンシのホームリージョン名を連絡する。

Dynamic GroupとRuntime Policyは [テナンシ管理者向けガイド](./public-deploy-tenancy-admin.md) の手順を使用する。

## 2. デプロイ担当グループのPolicy

専用コンパートメントの`manage all-resources`に加え、Resource Managerの画面表示やObject Storage namespace取得のため、次のtenancy-level read権限が必要になる。

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

`manage all-resources`がResource Managerを含む構成では後者2文は重複するが、権限レビューを分かりやすくするため明記してよい。

### 付与してはいけない権限

専用コンパートメント利用者には次を付与しない。

```text
Allow group <deployer-group> to manage all-resources in tenancy
Allow group <deployer-group> to manage domains in tenancy
Allow group <deployer-group> to manage policies in tenancy
```

## 3. デプロイ前チェック

- 管理者からIAM設定完了の連絡を受けた。
- Dynamic Group / Policy反映から5～10分待った。
- 専用コンパートメントだけを選択している。
- テナンシのホームリージョンを確認した。
- ADB、Container Instances、Functions、Identity Domainのservice limitを確認した。
- `enable_opensearch=true`にする場合は課金とservice limitを確認した。

## 4. Deploy to Oracle Cloud

1. READMEの**Deploy JetUse to Oracle Cloud**ボタンを開く。
2. Stack compartmentにJetUse専用コンパートメントを選択する。
3. Variableの`compartment_ocid`に同じ専用コンパートメントを指定する。
4. `enable_dynamic_group=false`にし、事前作成済みのDynamic Group名（runtime / ADB / Semantic Store）を入力する。
5. Runtime Policyが事前作成済みなら`enable_runtime_policy=false`、このコンパートメントでPolicyを管理できるなら`true`にする。
6. Identity Domain管理権限がない場合は、隔離検証用途に限り`enable_auth=false`にする。認証が必要な場合は管理者へDomain権限を依頼する。
7. Planを実行し、権限のないIAM作成が含まれないことと、作成先が専用コンパートメント内であることを確認する。
8. Applyを実行する。

## 5. デプロイ後チェック

1. Outputの`app_url`を開く。
2. `demo_username` / `demo_password`でログインする。
3. Chatで短いメッセージを送信する。
4. RAGへ小さいテキストファイルを登録し、Vector Store処理完了を確認する。
5. DB画面を開き、ADB接続を確認する。
6. Functions経由のAPI（presets等）を確認する。
7. 必要に応じてSpeech、OCR、翻訳を確認する。

## 6. 権限エラーの切り分け

| 症状 | 主な原因 | 連絡先 |
|---|---|---|
| Stackを作れない | Deployer GroupのORM権限不足 | テナンシ管理者 |
| PlanでObject Storage namespace取得失敗 | `read objectstorage-namespaces`不足 | テナンシ管理者 |
| Applyで特定リソースだけ403/404 | 専用コンパートメントの`manage all-resources`不足 | テナンシ管理者 |
| Apply成功後、Chat/RAG/OCR等が404 | Dynamic Group / Runtime Policy不足または反映待ち | テナンシ管理者 |
| API Gateway経由のFunctionsが500 | API Gateway→Functions Policy不足 | テナンシ管理者 |
| Policy作成が400 "No permissions found" | 入力した既存Dynamic Group名が存在しない | テナンシ管理者 |

問い合わせ時は、Stack OCID、Job OCID、失敗したTerraform resource名、OCIエラーコード、request IDを共有する。Terraform stateや生成パスワードは共有しない。

## 7. Destroy

`enable_dynamic_group=false`で事前作成IAMを参照した場合、そのDynamic GroupはStackのDestroy対象にならない。`enable_runtime_policy=true`で作成したPolicyは同じStackの管理対象となり、Destroyで削除される。

## 関連資料

- [Public版 IAM要件](./public-iam-requirements.md)
- [Dynamic Group compact構成](./dynamic-group-matching-rules.md)
- [Resource Managerデプロイ](./orm.md)
