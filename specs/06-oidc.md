# specs/06 — Phase 1 認証（INFRA-02: 専用Identity Domain + OIDC PKCE）

状態: ドラフト（2026-06-10作成）
仕様参照: specs/00-architecture.md（認証行）/ specs/03（jetuse_core/auth.py）/ specs/05（auth.tsx）

## [INFRA-02] 専用Identity DomainによるOIDC認証

### 決定事項（ユーザー指示 2026-06-10）

- **専用Identity Domainを新設**（Defaultは使わない）。配置は **jetuse-protoコンパートメント**
- 目的: JetUseアプリの**エンドユーザー認証**（Cognito User Pool相当）。アプリ利用者をテナンシ管理者の名簿から分離し、ログイン画面のリブランドも可能にする

### 構成

| 要素 | 内容 |
|---|---|
| Identity Domain | `{prefix}-domain`（freeタイプ、ap-osaka-1）。Terraform管理（modules/identity-domain） |
| アプリ登録 | SPA用 **publicクライアント**（PKCE必須、シークレットなし）。redirect: APIGWの `https://{gw}/` と `http://localhost:4173/`（開発用）。Terraform化できない場合はCLI(identity-domains API)→それも不可なら docs/setup/idcs.md の手作業手順 |
| グループ | `jetuse-users`（アプリ利用許可制御） |
| SPA | `oidc-client-ts` でAuthorization Code + PKCE。`VITE_OIDC_*` 環境変数（issuer/clientId）。トークンはメモリ+sessionStorage |
| API | APP-01の `jetuse_core/auth.py`（既実装）に issuer/JWKS URL を設定して有効化（`AUTH_REQUIRED=true`） |

### フロー

SPA → Domainログイン（PKCE）→ IDトークン/アクセストークン → `Authorization: Bearer` → API GW → FastAPI/FunctionsがJWKSで検証 → `sub` をユーザーIDとして利用

### 完了条件

- [ ] Domain作成（Terraform apply、ACTIVE）
- [ ] SPAアプリ登録 + `jetuse-users` グループ（作成方法を検証レポートに記録。手作業分は docs/setup/idcs.md）
- [ ] テストユーザーでPKCEログイン → チャット画面表示 → `AUTH_REQUIRED=true` のAPIがJWT検証を通して200（API GW経由E2E）
- [ ] 検証レポート docs/verification/INFRA-02.md

### トークン更新（INFRA-02b、不具合修正 2026-06-10）

実機症状: ログイン約1時間後（アクセストークンTTL）に「送信しても無反応（API全401）」「放置で画面が白くなる」。原因は (1) Reactのトークンが起動時スナップショットのまま更新されない、(2) oidc-client-ts既定の `automaticSilentRenew` が `redirect_uri`（=SPAルート）を隠しiframeで読み込み、**SPA全体がiframe内で再起動**して状態を壊すこと。

設計:

- silent renewは既定の有効のまま、**iframe内（`window.self !== window.top`）では `signinSilentCallback()` のみ実行しアプリを起動しない**（専用silent-redirect-uriを追加するとIdentity Domainのアプリ設定変更=人間承認が必要になるため、既存redirect URIを流用する方式を採る）
- `events.addUserLoaded` でReactのユーザー状態（accessToken）を更新し、更新後のAPIコールに新トークンを使う
- `events.addSilentRenewError` / `addAccessTokenExpired` で `signinRedirect()`（Domainのセッションが生きていれば無操作で復帰。サードパーティCookie遮断ブラウザでiframe更新が失敗するケースのフォールバックを兼ねる）
- 認可エラーリダイレクト（`?error=...`、prompt=none失敗等）はboot時に検出して通常ログインへ
- APIが401を返した場合は `reauthenticate()`（signinRedirect）で回復

### トークン更新v2（INFRA-02c、放置試験の不具合修正 2026-06-11）

放置試験の実機症状: 約5秒周期で「読み込み中⇔トップページ」を往復し、最終的に再ログイン画面に到達。原因は (1) oidc-client-tsの隠しiframe更新は失敗時に**既定で無制限に5秒間隔リトライ**（`maxSilentRenewTimeoutRetries`未設定時、実装で確認）、(2) INFRA-02bの expired/renewError→`signinRedirect()` 自動リダイレクトに**ループ保護がない**こと。iframe方式はSameSite Cookie・state共有・フレーム制限に依存し本質的に脆い。

設計（iframe全廃）:

- `automaticSilentRenew: false`。silent renew・隠しiframeは使わない
- `accessTokenExpiring`（期限60秒前）で**トップレベル`signinRedirect()`を単発実行**（Domainセッション生存中は無操作で復帰。第三者Cookie遮断の影響を受けない）
- **ループ保険**: 自動signinRedirectは `sessionStorage` 記録で**30秒に1回まで**。超過時は自動遷移せず「セッションの有効期限が切れました［再ログイン］」画面を表示（手動ボタン）
- `?error=` リダイレクトも同じガード経由。API 401時の `reauthenticate()` も同様
- 制約として明示: 自動再ログイン時にページ遷移が走るため未送信の入力は失われる（履歴はADB保存済み）。refresh token方式はDomain設定変更（人間承認）が必要なため次回判断

### 備考

- issuer/JWKS/client_idはすべて設定値。将来の専用Domain差替・顧客IdP連携は設定変更のみで可能な作りを維持する
- ユーザー作成（誰を招待するか）は人間の判断事項
