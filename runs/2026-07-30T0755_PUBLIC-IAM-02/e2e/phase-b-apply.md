# Phase B: 制限ユーザーによる plan / apply（十分性の検証）

実行者: `jetuse-deployer`（テナンシ権限ゼロ・API キー認証の CLI プロファイル）
配布物: `orm-main` リリースの `jetuse-orm.zip`（README のデプロイボタンが指す実物。
`image_tag` の既定は `96e73116f42e57102d86aef55421352c634ea8b0` = main HEAD に固定されていた）

## スタック変数

| 変数 | 値 |
|---|---|
| `compartment_ocid` | `jetuse-restricted` |
| `region` | `us-chicago-1` |
| `prefix` | `jetuse3` |
| `enable_dynamic_group` | `false` |
| `existing_dynamic_group` | `jetuse-restricted-dg` |
| `enable_runtime_policy` | `true` |
| `existing_iam_covers_hosted_agents` | `true` |
| その他 | 既定（`enable_auth=true` / `enable_hosted_agents=true` / `enable_semantic_store=true` / `enable_project_autocreate=true`） |

## plan

`SUCCEEDED`。**`Plan: 181 to add, 0 to change, 0 to destroy.`**

IAM 関連で作成対象に入ったもの（＝スタックがコンパートメント内に作るものだけ）:

```text
# module.iam.oci_identity_policy.runtime[0] will be created
# module.identity_domain[0].oci_identity_domain.this will be created
# module.identity_domain_app[0].oci_identity_domains_app.spa will be created
# module.identity_domain_app[0].oci_identity_domains_grant.demo will be created
# module.identity_domain_app[0].oci_identity_domains_setting.this will be created
# module.identity_domain_app[0].oci_identity_domains_user.demo will be created
# module.identity_domain_app[0].terraform_data.demo_password will be created
# module.hosted_agent[0].oci_identity_domains_app.agent will be created
```

`oci_identity_dynamic_group` と root の `oci_identity_policy.runtime_tenancy` は
**作成対象に現れない**（`enable_dynamic_group=false` のため `count=0`）。

## apply

`SUCCEEDED`。**`Apply complete! Resources: 181 added, 0 changed, 0 destroyed.`**
所要 17分（07:57:47 開始 → 08:14:22 終了）。エラー・警告なし。

主要な経過（ログ抜粋）:

```text
module.identity_domain[0].oci_identity_domain.this: Creation complete after 1m13s
module.identity_domain_app[0].oci_identity_domains_setting.this: Creation complete after 0s
module.identity_domain_app[0].oci_identity_domains_user.demo: Creation complete after 0s
module.identity_domain_app[0].terraform_data.demo_password: Provisioning with 'local-exec'...
module.identity_domain_app[0].terraform_data.demo_password: Creation complete after 3s
module.identity_domain_app[0].oci_identity_domains_app.spa: Creation complete after 0s
module.identity_domain_app[0].oci_identity_domains_grant.demo: Creation complete after 0s
module.hosted_agent[0].oci_identity_domains_app.agent: Creation complete after 1s
module.hosted_agent[0].terraform_data.agent["openai"]: Creation complete after 3m0s
module.hosted_agent[0].terraform_data.agent["langgraph"]: Creation complete after 3m0s
module.hosted_agent[0].terraform_data.agent["adk"]: Creation complete after 3m0s
time_sleep.iam_propagation[0]: Creating...   # ホスト型エージェント前の IAM 反映待ち(600s)。ADB 作成と並行
```

**事前作成していないポリシーを1つも足さずに完走した**。したがって Phase A の事前作成集合
（Dynamic Group 1本 + root の namespace 参照1文 + デプロイ担当への権限）は**十分**である。

## `UserPasswordChanger`（FIX-58 の要）が制限ユーザーで成立したことの裏取り

`terraform_data.demo_password` の local-exec は出力が抑止される（sensitive）ため、
ドメイン側の状態を SCIM で確認した。

```text
must-change: False | cant-change: False | last-successful-set: 2026-07-30T07:59:31.881Z
status: verified | can-use-console-password: True
```

さらに実ブラウザ（Chromium）で `demo` / スタック出力のパスワードでログインし、
パスワード変更を要求されずアプリのホーム画面（ユースケース一覧）まで到達することを確認した。

## Identity Domain 管理に必要だった権限

専用コンパートメントの `manage all-resources` のみ。**テナンシ側の Domain 管理権限も、
ドメイン管理者ロールの明示付与も不要**だった（ドメインを自分のコンパートメントに作成した
プリンシパルが、そのドメインの SCIM 管理 API を実行できた）。
