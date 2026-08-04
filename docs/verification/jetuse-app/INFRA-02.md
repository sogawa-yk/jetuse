# INFRA-02 検証レポート: 専用Identity Domain + OIDC(PKCE)

日付: 2026-06-10
仕様: specs/06-oidc.md
状態: **サーバー側E2E完了。ブラウザPKCEログインはユーザーのアカウント有効化待ち**

## 構築内容（すべてコード/CLIで再現可能 — 手作業ゼロ）

| 要素 | 内容 | 方法 |
|---|---|---|
| Identity Domain | `jetuse-dev-domain`（free、jetuse-protoコンパートメント、ap-osaka-1） | Terraform `modules/identity-domain` |
| SPAアプリ | `jetuse-dev-spa`（publicクライアント、authorization_code+refresh_token、redirect=API GWのURL） | `oci identity-domains app create` |
| M2Mテストアプリ | `jetuse-dev-m2m-test`（confidential、client_credentials。JWT検証の自動テスト用） | 同上 |
| グループ | `jetuse-users`（+ ユーザー1名を作成しメンバー追加。アクティベーションメール送信済み） | `oci identity-domains group/user` |
| ドメイン設定 | JWKS公開アクセス有効化 + CORS許可（API GWオリジン） | `oci identity-domains setting patch` |

## 実測E2E（API GW経由）

| ケース | 結果 |
|---|---|
| トークンなしで `/api/chat/ping` | **401**（fail-closed） |
| Identity Domain発行のJWT（client_credentials）付き | **200 + SSE成立**。`sub`（client_id）がユーザーIDとしてイベントに反映 |

バックエンドはAPP-01の `jetuse_core/auth.py` をそのまま利用（コード変更なし、環境変数の設定のみ）:
`AUTH_REQUIRED=true` / `OIDC_ISSUER=https://identity.oraclecloud.com/` / `OIDC_JWKS_URL=<domain>/admin/v1/SigningCert/jwk`

## 実機で確定したハマりどころ（未文書/見落としやすい）

1. **Identity DomainのCREATEはホームリージョン必須**（大阪で実行すると `403 Please go to your home region IAD`）→ Terraformにhomeリージョンのproviderエイリアスを追加して解決
2. **OIDC issuerはドメインURLではなく汎用の `https://identity.oraclecloud.com/`**（discovery文書で確認）。バックエンドのissuer検証はこの値にする
3. **JWKSエンドポイントは既定で要認証（401）** → Settings `signingCertPublicAccess=true` で公開化（PyJWKClientの前提）
4. **SPAのトークン交換はCORS設定必須** → Settings `cloudGateCorsSettings` にAPI GWオリジンを許可（preflight 204確認）
5. アプリ登録のredirectURIは**https必須**（`http://localhost` も拒否）→ ローカル開発時も実環境URLでログインする運用

## 残課題

- [ ] ブラウザPKCEログインE2E: ユーザーがアクティベーションメールからパスワード設定 → API GWのURLでログイン → チャット画面表示（SPAは認証ONでデプロイ済み）
- [ ] アクセストークンのaudience/scope検証（現状は署名+issuer+期限のみ。Phase 8で強化）
- [ ] アプリ登録のTerraform化検討（現状CLI。oci providerのidentity_domains_appリソースで置換可能か）
- [ ] ログイン画面のリブランド（Domainのブランディング設定。任意）
