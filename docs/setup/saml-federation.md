# 【人間作業】SAMLフェデレーション手順書（SEC-01・起草版）

jetuse-dev-domain（IAM Identity Domain）を**SP**、社内IdP（Entra ID / Google Workspace）を**IdP**として
SAMLフェデレーションし、社内アカウントでSPAへSSOログインできるようにする。

> 状態: **起草（2026-06-13）— 実機検証待ち**。Identity Domain側・IdP側ともコンソール作業のため人間が実施。
> 検証後、画面名の差異・ハマりどころをこの文書に追記すること。

## 0. 前提と全体像

```
ブラウザ → SPA → (PKCE) → jetuse-dev-domain ──SAML──> Entra ID / Google Workspace
                              │ JITプロビジョニング(初回ログインでユーザー自動作成)
                              └ jetuse-usersグループへ自動割当 → 既存の認可がそのまま機能
```

- アプリ側(SPA/API)は**変更不要**（OIDC/JWTの発行者はjetuse-dev-domainのまま。フェデレーションは
  Domain内の認証手段が増えるだけ）
- 切り戻し: IdPポリシーでローカル（ユーザー名/パスワード）を併存させておけば、IdP障害時も
  管理者はローカルでログイン可能（**手順中はローカル併存を必ず維持**）

## 1. Identity Domain側（共通・前半）: SPメタデータの取得

1. OCIコンソール → アイデンティティとセキュリティ → **ドメイン** → `jetuse-dev-domain`
2. **セキュリティ → アイデンティティ・プロバイダ** → 「IdPの追加」→ **SAML IdPの追加**
3. 名前（例: `entra-id` / `google-ws`）を入力して進み、いったん**SPメタデータをエクスポート**
   - 控える値（メタデータXMLに含まれる）:
     - **プロバイダID（SP Entity ID）**: `https://idcs-xxxx.identity.oraclecloud.com:443/fed`
     - **アサーション・コンシューマURL（ACS）**: `https://idcs-xxxx.identity.oraclecloud.com/fed/v1/sp/sso`
4. この画面は「IdPメタデータ待ち」で保留し、IdP側設定（§2 or §3）へ

## 2. Entra ID側

1. Microsoft Entra管理センター → **エンタープライズ アプリケーション → 新しいアプリケーション →
   独自のアプリケーションの作成**（非ギャラリー）。名前例: `OCI jetuse-dev-domain`
2. 作成したアプリ → **シングル サインオン → SAML**
3. 「基本的なSAML構成」:
   - 識別子（エンティティID）= §1のSP Entity ID
   - 応答URL（ACS URL）= §1のACS URL
4. 「属性とクレーム」: **一意のユーザー識別子（NameID）= `user.mail`**
   （形式: 電子メールアドレス。Identity Domain側のプライマリ・メールと一致させる）
5. 「SAML証明書」から**フェデレーション メタデータ XML**をダウンロード
6. 「ユーザーとグループ」で利用ユーザー/グループを割り当て

## 3. Google Workspace側

1. 管理コンソール → アプリ → **ウェブアプリとモバイルアプリ → アプリを追加 → カスタムSAMLアプリの追加**
2. 表示名を入れ、**IdPメタデータをダウンロード**
3. サービスプロバイダの詳細:
   - ACS URL = §1のACS URL / エンティティID = §1のSP Entity ID
   - 名前ID: 形式=EMAIL、値=基本情報のメインのメールアドレス
4. 公開設定: 対象の組織部門/グループで**オン**にする

## 4. Identity Domain側（共通・後半）: IdP登録の完了

1. §1の画面に戻り、**IdPメタデータXMLをインポート**（Entra=フェデレーションメタデータ / Google=IdPメタデータ）
2. ユーザー・マッピング: 「**受信したNameID（メール）⇔ Identity Domainユーザーのプライマリ・メール**」
3. 作成後、IdPの詳細画面で**テスト・ログイン**（成功するまで有効化しない）
4. **JITプロビジョニング**を有効化（IdP詳細 → JIT構成）:
   - 「ユーザーが存在しない場合に作成」= 有効
   - 属性マッピング: email→ユーザー名/メール、givenName/surname→氏名
   - **グループ割当: `jetuse-users` を既定で付与**（SPAアプリの割当グループ。これが無いとログイン後403相当）
5. **セキュリティ → IdPポリシー**: 既定ポリシー（またはSPAアプリに紐づくポリシー）の
   「アイデンティティ・プロバイダの割当」に新IdPを追加
   - **ローカル（ユーザー名/パスワード）は残す**（切り戻し用。削除しない）
6. 動作確認: シークレットウィンドウでSPAのURLへ → ログイン画面にIdPボタンが出る →
   社内アカウントでログイン → SPA表示、`/api/conversations` 等が200

## 5. 検証チェックリスト（人間実機）

- [ ] Entra/Googleの未割当ユーザーが**拒否**される
- [ ] 初回ログインでユーザーがJIT作成され、jetuse-usersに入る
- [ ] 2回目以降のログインで同一ユーザーに紐づく（重複作成されない）
- [ ] IdPを一時停止してもローカル管理者でログインできる（切り戻し）
- [ ] JWTの`sub`が安定している（アプリのowner分離が初回/2回目で変わらないこと）

## 6. 既知の注意点（一般論 — 実機検証で更新すること）

- **時刻ずれ**: SAMLアサーションの有効期間は短い。IdP/SP双方NTP同期が前提
- **NameID不一致**: Entraで`user.userprincipalname`を使うとメールと異なる場合がある（`user.mail`推奨）
- 証明書ローテーション: IdP署名証明書の更新時はメタデータ再インポートが必要（Entraは期限通知を設定）
- Identity Domainの**サインイン画面にIdPボタンが出ない**場合はIdPポリシーの割当漏れを疑う
- 監査: ドメインの「監査イベント」でSSO成否を確認できる（トラブルシュートの起点）

---

## 付録（GAP-02、2026-06-13）: OCI内2ドメインでの検証経路（外部IdP不要）

外部IdP(Entra/Google)を用意せずSAMLフェデレーションを検証する手順。実機で構成実証済み。

1. IdP役のIdentity Domainをホームリージョンに作成（free可、`--is-hidden-on-login true`）
2. IdP役ドメインで `setting put --signing-cert-public-access true --csr-access none`
   → `/fed/v1/metadata` が公開され取得可能に
3. IdPメタデータから entityID / SSO(HTTP-Redirect) URL / 署名証明書(X509、**空白除去**)を抽出
4. SP(jetuse-dev-domain)で `identity-domains identity-provider create`:
   `--partner-provider-id <IdP entityID>` `--idp-sso-url` `--signing-certificate <空白除去cert>`
   `--name-id-format ...emailAddress` `--user-mapping-method NameIDToUserAttribute`
   `--user-mapping-store-attribute emails.value` `--jit-user-prov-enabled true`
   `--jit-user-prov-create-user-enabled true` `--jit-user-prov-assigned-groups [{value:<jetuse-users id>}]`
5. IdP役ドメインに SP表現のSAMLアプリ（CustomSAMLApp）を登録（audience=SP entityID, ACS=SP ACS）
6. SP側のサインオンポリシーにこのIdPを追加（ローカルは残す=切り戻し）+ IdP役にテストユーザー作成
7. ブラウザでSP(SPA)へアクセス→IdPボタン→テストユーザーでSSO→JIT作成を確認

> 5〜7はコンソール操作 or 対話が現実的。1〜4はCLIで自動化可能（GAP-02で実証）。
