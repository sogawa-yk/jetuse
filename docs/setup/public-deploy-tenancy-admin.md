# JetUse Public版デプロイガイド: テナンシ管理者

このガイドは、Dynamic GroupとIAM Policyを作成できるテナンシ管理者向けである。IAMとJetUseアプリは1つのResource Manager Stackで作成する。

テナンシ管理権限がなく、専用コンパートメントだけを管理する場合は [専用コンパートメント管理者向けガイド](./public-deploy-dedicated-compartment.md) を使用する。

## 全体フロー

```text
Deploy JetUse to Oracle Cloud
  └─ 1つのStack
       ├─ Dynamic Group / Runtime Policy
       ├─ VCN / ADB / Object Storage
       ├─ Container Instance / Functions / API Gateway
       └─ Identity Domain / OIDC
  ↓ IAM反映・ADB初期化
app_urlで可動確認
```

## 1. 実行ユーザーの権限

Administratorsグループのメンバーは通常追加設定不要。委任する場合は、実行ユーザーに次の操作を許可する。

- Dynamic Groupの管理
- root compartmentとJetUse専用コンパートメントのPolicy管理
- JetUse専用コンパートメントのResource Manager Stack / Job管理
- JetUse専用コンパートメントのアプリリソース管理
- Identity Domain管理（`enable_auth=true`の場合）

詳細は [Public版IAM要件](./public-iam-requirements.md) を参照。

## 2. デプロイ

1. JetUse専用コンパートメントを作成または選択する。
2. READMEの**Deploy JetUse to Oracle Cloud**ボタンを開く。
3. Stack / resource compartmentにJetUse専用コンパートメントを指定する。
4. `home_region`にテナンシのホームリージョンを指定する。
5. IAM設定は次の既定値を使用する。

   ```text
   enable_dynamic_group  = true
   enable_runtime_policy = true
   enable_semantic_store = true   # SQL Searchを使わない場合はfalse
   ```

6. PlanでIAMの作成先とアプリの作成先を確認する。
7. Applyする。

権限がないIAM操作はOCIの`403`になる。権限を追加しない場合は、管理者が該当IAMを事前作成してフラグを`false`にする。

## 3. Apply後の確認

1. Runtime / ADB / Semantic StoreのDynamic Groupを確認する。
2. Runtime PolicyのコンパートメントOCIDを確認する。
3. root compartmentのPolicyがObject Storage namespaceのread 1文だけであることを確認する。
4. IAM反映のため5〜10分待つ。
5. Outputの`app_url`を開き、`demo_username` / `demo_password`でログインする。

## 4. 可動テスト

| テスト | 確認するPolicy |
|---|---|
| Chat送信 | Generative AI |
| RAGファイル登録・検索 | Vector Store / File / Object Storage |
| DB画面・Select AI | ADB / ADB Resource Principal |
| Presets等のFunctions API | API Gateway→Functions |
| 議事録 / TTS | Speech / Object Storage / tag namespace |
| OCR / 翻訳 | Document / Language |
| SQL Search | Semantic Store / DB Tools / Secret / Database metadata |

`404 NotAuthorizedOrNotFound`が出た場合は、resource type、Dynamic Group membership、Policy配置先、コンパートメントOCID、`prefix`、IAM反映待ちを確認する。

## 5. Destroy

IAMも同じStackのstateに含まれる。Destroyするとアプリリソースと共に、このStackが作成したDynamic Group / Policyも削除対象になる。共有IAMを残す必要がある場合は、Destroy前にstateを移管する。

## 関連資料

- [専用コンパートメント管理者向けガイド](./public-deploy-dedicated-compartment.md)
- [Public版IAM要件](./public-iam-requirements.md)
- [Resource Managerデプロイ](./orm.md)
