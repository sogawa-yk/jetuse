# OCI Resource Manager ワンクリックデプロイ（INFRA-03）

README の「Deploy to Oracle Cloud」ボタンから、OCI Resource Manager(ORM) スタックとして
JetUse を一括デプロイする仕組みのリファレンス。スタック定義は `infra/orm/`。

## ボタンの動作

ボタンURLは `https://cloud.oracle.com/resourcemanager/stacks/create?zipUrl=<main.zipアーカイブ>`。
ORM が公開リポジトリ `main` の zip を取得し、スタック作成ウィザードを開く。
**作業ディレクトリに `infra/orm` を選択**（zip内にネストするため）。`schema.yaml` が入力UIを生成する。

入力は **コンパートメントのみ必須**。パスワード類は `random_password` で自動生成し、
コンテナイメージは公開GHCRの既定値を使う。Apply で以下を作成:

- ネットワーク（VCN/サブネット/NSG/ゲートウェイ各種）
- Autonomous Database 26ai（mTLS）＋ ウォレット生成
- Object Storage（spa / app-data / speech）＋ SPA配信用PAR
- Container Instance（FastAPI / 公開イメージ）、Functions（ルーター / 公開イメージ）
- API Gateway（`/api/*`→CI・Functions、`/`→SPAバケット）
- Identity Domain ＋ **OIDCアプリ(PKCE/public) ＋ デモユーザー**（`enable_auth=true` 時）
- IAM 動的グループ＋ポリシー（GenAI/ADB/Object/Secret）
- ADBウォレット・SPA(dist)・`config.json` を Terraform がバケットへアップロード

## 「ORMはTerraformのみ」をどう乗り越えているか（4つの自動化）

1. **コンテナイメージ**: `.github/workflows/release.yml` が main への push で API/fn-router を
   **GHCR(public)** へ publish。ORM既定の `api_image_url`/`fn_router_image` が公開イメージを指す。
   Container Instance は公開イメージを認証なしで pull。
2. **DB初期化**: `packages/api/entrypoint.sh` が `RUN_DB_BOOTSTRAP=true` のとき
   `jetuse_core/bootstrap.py` を実行。ADMINでウォレット取得→`JETUSE_APP`/`JETUSE_QUERY` 作成・権限・
   ネットワークACL→`ENABLE_RESOURCE_PRINCIPAL`→`migrate` を**冪等**に実施（ADB ACTIVE待ちリトライ付き）。
3. **SPA配信 + OIDC client_id**: SPAは実行時に `/config.json` を読む（`packages/web/src/auth.tsx`）。
   Terraform が コミット済み `packages/web/dist`（release.ymlが更新）と、OIDCアプリの client_id を
   含む `config.json` をバケットへアップロード（`infra/orm/spa.tf`）。
   → Container Instance は client_id に依存せず（issuer/JWKSのみ）、依存の循環を回避。
4. **OIDC自動登録**: `infra/terraform/modules/identity-domain-app` が `oci_identity_domains_app`
   (PKCE/public)・デモユーザー・付与を作成。redirect は API Gateway ホスト。

## 前提・初回セットアップ（1回のみ）

- 本リポジトリを **public** にする（ORMがzipを取得するため）。
- GHCR パッケージ `jetuse-api` / `jetuse-fn-router` を **public** に設定（初回 push 後に
  GitHub → Packages → Package settings、または `gh api -X PATCH /user/packages/container/<name> -f visibility=public`）。
- デプロイ実行者は **テナンシ管理者**（IAM動的グループはテナンシレベル）。
- Generative AI 提供リージョン（大阪 ap-osaka-1 等）を選ぶ。

## デプロイ後

1. 出力 `app_url` を開く。初回は ADB 初期化のため数分〜15分は DB系が 503 になりうる。
2. `enable_auth=true` の場合、`demo_username`/`demo_password`(出力) でログイン。

## 残存事項・既知のリスク（フォールバック）

- **OIDC（最高リスク）**: `oci_identity_domains_app` の PKCE 設定はプロバイダ差で
  「ログインは通るがトークン拒否」等が起こりうる。うまくいかない場合は **`enable_auth=false`**
  でデプロイすれば認証なしで完全に使える状態になる（OIDCアプリ/ユーザーは作成されない）。
  PKCEを実機確認後に `enable_auth=true` を既定にする運用。
- **Select AI クレデンシャル**: APIキー版 `JETUSE_OCI_CRED` は RP 環境で作れないため、bootstrap が
  `ENABLE_RESOURCE_PRINCIPAL` で `OCI$RESOURCE_PRINCIPAL` を有効化（`SELECT_AI_CREDENTIAL` で参照）。
  ADBのリソースプリンシパルでGenAIを呼ぶには IAM 側の許可が要る（iamモジュールで付与）。
  失敗時も chat / RAG(Vector Store/OpenSearch) 等のコア機能は動作する。
- **SPA dist の鮮度**: `packages/web/dist` は **生成物**（release.yml が main で再生成・コミット）。
  手編集しない。フロント変更は push 後に release.yml が dist を更新する。
- **GHCR公開pull不可の場合**: 既定イメージURLを OCIR public 等へ変更（schema の image URL を上書き）。

## ローカル検証

```bash
cd infra/orm
terraform init -backend=false && terraform validate   # 静的検証
# 使い捨てコンパートメントで実検証(ambient auth):
terraform init && terraform apply -var compartment_ocid=<OCID> -var tenancy_ocid=<OCID> -var region=<region>
# 確認後:
terraform destroy -var ...
```

## 関連ファイル

- `infra/orm/` … ORMスタック（`main.tf`/`locals.tf`/`spa.tf`/`variables.tf`/`outputs.tf`/`providers.tf`/`schema.yaml`）
- `infra/terraform/modules/identity-domain-app/` … OIDCアプリ＋デモユーザー
- `infra/terraform/modules/adb/` … ウォレット生成出力を追加
- `packages/api/entrypoint.sh` / `jetuse_core/bootstrap.py` … DB自己ブートストラップ
- `packages/web/src/auth.tsx` … 実行時 `/config.json` 読み込み
- `.github/workflows/release.yml` … 公開イメージ＋dist
