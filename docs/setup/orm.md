# OCI Resource Manager で JetUse Public 版をデプロイ

GitHub の **Deploy to Oracle Cloud** ボタンから、JetUse を OCI Resource Manager（ORM）スタックとして構築する。通常の利用者にテナンシ管理権限を要求しないよう、IAM とアプリ本体を二つのスタックに分離している。

| 段階 | Deployパッケージ | 実行者 | 頻度 |
|---|---|---|---|
| IAM Bootstrap | `jetuse-iam-bootstrap.zip` | テナンシ IAM 管理者 | 対象コンパートメントごとに1回 |
| JetUse 本体 | `jetuse-orm.zip` | `JetUseDeployers` 等の通常ユーザー | 環境ごと |

## 1. 管理者: IAM Bootstrap

最初に [iam.md](./iam.md) の手順で runtime Dynamic Group / Policy と、通常デプロイ担当グループの Policy を作成する。管理者自身が毎回 JetUse をデプロイする必要はない。

READMEの**Deploy JetUse IAM Bootstrap to Oracle Cloud**ボタンを使用する。専用ZIPの直下にTerraformと`schema.yaml`があるため、Working directoryの指定は不要。Apply後はIAMの反映を数分待つ。

## 2. 通常利用者: Deploy to Oracle Cloud

READMEの**Deploy JetUse to Oracle Cloud**ボタンは、次のURLで公開`main`ブランチから生成された専用ZIPをORMへ渡す。

```text
https://cloud.oracle.com/resourcemanager/stacks/create?zipUrl=https://github.com/sogawa-yk/jetuse/releases/download/orm-main/jetuse-orm.zip
```

作成ウィザードで以下を指定する。

1. Stack compartment に Bootstrap と同じ JetUse 専用コンパートメントを選ぶ。
2. 変数画面で同じコンパートメントを選ぶ。`prefix` は識別しやすいよう Bootstrap と同じ値を推奨する。
3. `home_region` は OCI Console のテナンシ詳細に表示される Home region を選ぶ。現在 Console で開いているリージョンとは限らない。
4. Plan の作成物と課金対象を確認して Apply する。

Resource Manager が自動入力するリージョンはリソースの配備リージョンであり、テナンシのホームリージョンではない。そのため `home_region` は必須の画面入力としている。

## 入力

| 入力 | 必須 | 説明 |
|---|---|---|
| `compartment_ocid` | Yes | Bootstrap と同じ JetUse 専用コンパートメント |
| `home_region` | Yes | Identity Domain の作成に使うテナンシホームリージョン |
| `prefix` | Yes | リソース名。Bootstrap と同じ値を推奨。既定 `jetuse` |
| `demo_email` | Yes（認証時） | 初期デモユーザーのメール |
| `adb_admin_password` | No | 空なら安全なランダム値を生成 |
| `enable_opensearch` | No | 常設課金が発生するため既定 false |
| `ocir_*` / image URL | 通常は変更不要 | Public 版の公開 OCIR image を参照 |

`enable_iam` はアプリスタックから削除した。IAM をここで有効にして通常利用者の Apply が途中で失敗する構成には戻さない。

## 作成されるリソース

- VCN、public/private subnet、NSG、Internet/NAT/Service Gateway
- Autonomous Database 26ai（mTLS）と wallet
- Object Storage（SPA、app-data、speech）と SPA 配信用 PAR
- Container Instance（FastAPI）と OCI Functions（router）
- API Gateway（`/api/*` と SPA）
- Logging log group / logs、Monitoring 送信先
- Identity Domain、OIDC public client（PKCE）、初期デモユーザー
- 任意の OpenSearch cluster
- ADB wallet、SPA、runtime `config.json` の Object Storage 配置

Dynamic Group と IAM Policy は作らない。Bootstrap stack が所有するため、本体 stack を Destroy しても IAM は残り、同じコンパートメントへの再デプロイで再利用できる。

## デプロイ後

1. Output の `app_url` を開く。
2. `demo_username` / `demo_password` でログインする。
3. 初回は ADB 作成と DB bootstrap に 10〜15 分程度かかり、その間 DB 系 API が一時的に 503 になることがある。

JetUse のエンドユーザーは OCI Console のアカウントや IAM Policy を必要としない。OIDC ユーザーの追加・運用は作成された Identity Domain 内で行う。

## 自動化の仕組み

1. **Deploy packages**: `.github/workflows/release.yml` がTerraformと`schema.yaml`をルートに持つ2つの専用ZIPを`orm-main`リリースへpublishする。
2. **Container images**: 同workflowがPublic `main` の API / Functions image を公開 OCIR に publish する。
3. **DB bootstrap**: `packages/api/entrypoint.sh` が初回起動時に ADB user、権限、schema migration を冪等に作成する。
4. **SPA と OIDC**: Terraform が `packages/web/dist` と OIDC client ID を含む `config.json` を Object Storage に配置する。
5. **OIDC registration**: Identity Domain、PKCE client、初期ユーザーと grant を Terraform で作成する。
6. **Runtime authorization**: Container Instances / Functions / ADB は Bootstrap で作られた resource principal Policy を使用する。

## セキュリティ上の注意

- JetUse 専用コンパートメントを使う。デプロイ担当グループはそのコンパートメント内でリソースを管理できる。
- ORM state / job output には ADB とデモユーザーの生成パスワードが含まれる。Stack / Job を読めるグループを限定する。
- `enable_auth=true` が Public 標準。認証を無効にすると API が公開状態になるため、隔離した検証環境以外では使用しない。
- `enable_opensearch=true` は常設課金と service limit を Plan 前に確認する。
- Bootstrap と異なる対象コンパートメントへ本体を作ると resource principal が Policy に入らない。`prefix` は権限判定には使わないが、運用上は揃える。

## ローカル静的検証

```bash
terraform -chdir=infra/orm-bootstrap init -backend=false
terraform -chdir=infra/orm-bootstrap validate

terraform -chdir=infra/orm init -backend=false
terraform -chdir=infra/orm validate
```

実際の `plan` / `apply` は対象テナンシの権限と値が必要。IAM Bootstrap の Apply と本番相当リソースの Apply は組織の承認手順に従う。

## 関連ファイル

- `infra/orm-bootstrap/`: 管理者向け IAM stack
- `infra/orm/`: 通常利用者向け JetUse stack
- `infra/terraform/modules/iam/`: Dynamic Group / Policy の正本
- `scripts/package-orm-stacks.sh`: Deploy専用ZIPの生成
- [iam.md](./iam.md): 権限一覧、手動設定、トラブルシュート
- `.github/workflows/release.yml`: Public image と SPA dist の release
