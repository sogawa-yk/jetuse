# JetUse Public版デプロイガイド: テナンシ管理者

このガイドは、Dynamic GroupとIAM Policyを作成できるテナンシ管理者向けである。IAMをBootstrapした後、同じ管理者または専用コンパートメント利用者がJetUseアプリをデプロイする。

テナンシ管理権限がなく、専用コンパートメントだけを管理する場合は [専用コンパートメント管理者向けガイド](./public-deploy-dedicated-compartment.md) を使用する。

## 全体フロー

```text
Stage 0: テナンシ管理者の既存権限
  ↓
Stage 1: IAM Bootstrap
  ├─ Dynamic Group
  ├─ Runtime Policy
  └─ Deployer Group Policy
  ↓ 5～10分待機
Stage 2: JetUseアプリ（infra/orm）
  ↓
Stage 3: Resource Principal / E2E確認
```

IAM Bootstrapとアプリ本体は別のStageにする。アプリ本体の`infra/orm`でIAMを作成すると、通常デプロイ担当者にも`manage domains` / `manage policies`が必要になるため禁止する。

## 1. Bootstrap実行者の権限

Administratorsグループのメンバーは通常追加不要。Bootstrapを委任する場合は、少なくとも次が必要になる。

```text
Allow group <bootstrap-admin-group> to manage domains in tenancy
Allow group <bootstrap-admin-group> to manage policies in tenancy
Allow group <bootstrap-admin-group> to inspect compartments in tenancy
Allow group <bootstrap-admin-group> to manage orm-stacks in compartment id <compartment_ocid>
Allow group <bootstrap-admin-group> to manage orm-jobs in compartment id <compartment_ocid>
```

BootstrapはIAMのホームリージョンで実行する。

## 2. 専用コンパートメントと通常グループ

1. JetUse専用コンパートメントを作成または選択する。
2. 既存の通常IAMグループ（例: `Default/JetUseDeployers`）を選択する。
3. JetUseをデプロイするユーザーをそのグループへ追加する。
4. コンパートメントに他システムの本番データを配置しない。

## 3. Dynamic Group

1環境につきDynamic Groupを1個作るcompact構成では、次のMatching Ruleを使用する。

名前の例:

```text
jetuse-<environment>-dg
```

Matching Rule:

```text
Any {
  all {
    resource.type='computecontainerinstance',
    resource.compartment.id='<compartment_ocid>'
  },
  all {
    resource.type='fnfunc',
    resource.compartment.id='<compartment_ocid>'
  },
  all {
    resource.type='autonomousdatabase',
    resource.compartment.id='<compartment_ocid>'
  },
  all {
    resource.type='generativeaisemanticstore',
    resource.compartment.id='<compartment_ocid>'
  }
}
```

社内dev/public共有とdeploy-test専用の具体的なルールは [Dynamic Group compact構成](./dynamic-group-matching-rules.md) を参照する。

## 4. Dynamic GroupのRuntime Policy

compact構成では、次のすべての文の`<jetuse-dg>`に同じDynamic Group名を指定する。Policyの配置先は原則JetUse専用コンパートメントとする。

```text
Allow dynamic-group <jetuse-dg> to use generative-ai-family in compartment id <compartment_ocid>
Allow dynamic-group <jetuse-dg> to manage generative-ai-vectorstore in compartment id <compartment_ocid>
Allow dynamic-group <jetuse-dg> to manage generative-ai-vectorstore-file in compartment id <compartment_ocid>
Allow dynamic-group <jetuse-dg> to manage generative-ai-file in compartment id <compartment_ocid>
Allow dynamic-group <jetuse-dg> to use autonomous-database-family in compartment id <compartment_ocid>
Allow dynamic-group <jetuse-dg> to manage objects in compartment id <compartment_ocid>
Allow dynamic-group <jetuse-dg> to read buckets in compartment id <compartment_ocid>
Allow dynamic-group <jetuse-dg> to manage ai-service-speech-family in compartment id <compartment_ocid>
Allow dynamic-group <jetuse-dg> to use ai-service-document-family in compartment id <compartment_ocid>
Allow dynamic-group <jetuse-dg> to use ai-service-language-family in compartment id <compartment_ocid>
Allow dynamic-group <jetuse-dg> to read tag-namespaces in compartment id <compartment_ocid>
Allow dynamic-group <jetuse-dg> to use log-content in compartment id <compartment_ocid>
Allow dynamic-group <jetuse-dg> to use metrics in compartment id <compartment_ocid>
Allow dynamic-group <jetuse-dg> to read secret-family in compartment id <compartment_ocid>
Allow dynamic-group <jetuse-dg> to use database-tools-family in compartment id <compartment_ocid>
Allow dynamic-group <jetuse-dg> to read database-family in compartment id <compartment_ocid>
```

compact構成では、`manage objects`が`read objects`を、`use autonomous-database-family`が`read autonomous-database-family`を含むため、重複するread文は省略している。

Object Storage namespaceはtenancy-scopeなので、root compartmentのPolicyへ次を設定する。

```text
Allow dynamic-group <jetuse-dg> to read objectstorage-namespaces in tenancy
```

API GatewayはDynamic Groupへ含めず、JetUse専用コンパートメントのPolicyへ次を設定する。

```text
Allow any-user to use functions-family in compartment id <compartment_ocid>
where all {
  request.principal.type='ApiGateway',
  request.resource.compartment.id='<compartment_ocid>'
}
```

### compact構成のトレードオフ

Container Instance、Functions、ADB、Semantic Storeが権限の和集合を持つ。Dynamic Group上限を優先する構成であり、厳密な最小権限が必要になった場合はRuntime / ADB / Semantic Storeを分離する。

## 5. デプロイ担当グループのPolicy

```text
Allow group <deployer-group> to inspect compartments in tenancy
Allow group <deployer-group> to inspect tenancies in tenancy
Allow group <deployer-group> to read objectstorage-namespaces in tenancy
Allow group <deployer-group> to manage orm-stacks in compartment id <compartment_ocid>
Allow group <deployer-group> to manage orm-jobs in compartment id <compartment_ocid>
Allow group <deployer-group> to manage all-resources in compartment id <compartment_ocid>
```

`manage all-resources`を`in tenancy`にしない。通常デプロイ担当者へ`manage domains` / `manage policies`を付与しない。

## 6. IAM反映確認

1. Dynamic GroupのMatching Ruleを再表示する。
2. Runtime PolicyのDynamic Group名とコンパートメントOCIDを確認する。
3. Deployer Policyの通常グループ名を確認する。
4. tenancy-level PolicyがObject Storage namespaceのreadだけであることを確認する。
5. IAM反映のため5～10分待つ。

## 7. JetUseアプリのデプロイ

1. READMEの**Deploy to Oracle Cloud**ボタンを開く。
2. Working directoryに`infra/orm`を指定する。
3. Stack / resource compartmentに同じJetUse専用コンパートメントを指定する。
4. `home_region`にテナンシのホームリージョンを指定する。
5. Planを確認してApplyする。

compact Dynamic Groupを手動作成済みの場合、現行の`infra/orm-bootstrap`は実行しない。現行実装はRuntime / ADB / Semantic Storeを分けるstrict構成であり、追加のDynamic Groupを作成する。compact / existing-group対応が実装された後にBootstrapへ切り替える。

## 8. 可動テスト

次の順に確認する。

| テスト | 確認するPolicy |
|---|---|
| Chat送信 | Generative AI |
| RAGファイル登録・検索 | Vector Store / File / Object Storage |
| DB画面・Select AI | ADB / ADB Resource Principal |
| Presets等のFunctions API | API Gateway→Functions |
| 議事録 / TTS | Speech / Object Storage / tag namespace |
| OCR | Document Understanding |
| OCI Language翻訳 | Language |
| 管理画面のログ・メトリクス | Logging / Monitoring |
| SQL Search | Semantic Store / DB Tools / Secret / Database metadata |

404 `NotAuthorizedOrNotFound`が出た場合は、resource type、Dynamic Group membership、Policy配置先、コンパートメントOCID、IAM反映待ちを確認する。

## 9. DestroyとIAMの扱い

アプリStackのDestroy後もDynamic GroupとRuntime Policyは残す。同じ専用コンパートメントで再デプロイする際に再利用できる。専用コンパートメントを廃止する場合だけ、アプリをDestroyした後にIAM Policy、Dynamic Groupの順で削除する。

## 関連資料

- [専用コンパートメント管理者向けガイド](./public-deploy-dedicated-compartment.md)
- [Public版 IAM要件](./public-iam-requirements.md)
- [Dynamic Group compact構成](./dynamic-group-matching-rules.md)
- [Resource Managerデプロイ](./orm.md)
