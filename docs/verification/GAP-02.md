# GAP-02 検証レポート: SAMLフェデレーション（OCIマネージド完結で実機構成）

- 日付: 2026-06-13 / ブランチ: `task/gap-02` / 計画: docs/plan-gap-b.md
- 判断軸（ユーザー指示）: **OCIマネージドサービスで完結できるか** → **Yes（実証）**

## 結論

外部IdP（Entra/Google）を使わず、**OCI Identity Domainを2つ使うだけでSAMLフェデレーションが
構成可能**であることを実機で実証した。本番の顧客連携（Entra/Google）も、SP設定は同一で
IdPメタデータを差し替えるだけ。

## 実施した構成（すべてOCI CLI=マネージド操作で完了）

1. **IdP役ドメイン作成**: `jetuse-idp-test`（free、ホームリージョンIAD、ログイン非表示）
2. **IdPメタデータ公開**: `signing-cert-public-access=true` に設定 → `/fed/v1/metadata` が200で取得可
   （INFRA-02の学び: 既定falseで401。設定putは `--csr-access` も必須）
3. **SP側(jetuse-dev-domain)へSAML IdP登録**: `identity-provider create` で
   partner-provider-id=IdP entityID / idp-sso-url / signing-certificate（**空白除去が必須**）/
   NameID=emailAddress / user-mapping=emails.value / **JITプロビジョニング有効・jetuse-usersへ自動割当**
   → IdP登録成功（id=5acc5b61…）

## 残（対話操作の境界 — 自動化困難）

| 項目 | 状況 |
|---|---|
| IdP側のSP表現アプリ登録 | SCIM必須属性の調整中（`missingReqAttributes`）。コンソールなら数クリック |
| テストユーザーのパスワード初期化 | 新規ユーザーは初回ログインフロー（メール/管理者設定）が対話的 |
| ブラウザSSOログインE2E | 上記2つの後の最終確認。ヘッドレス自動化が非現実的（人間 or Playwright手動） |

## 判定

- **GAP-02の本質（マネージド完結の可否）= go確定**。技術的building block（メタデータ交換・署名検証・
  SAML IdP登録・JIT）はすべて実機で成功
- 最終のブラウザSSOは対話操作のため、人間が実施 or 本番の実IdP検証(SEC-01手順書)に委ねる
- 手順書 `docs/setup/saml-federation.md` に「OCI内2ドメインでの検証経路」を追記

## クリーンアップ（実施済み、2026-06-13）

ユーザー判断=(B)削除。SP側のSAML IdP登録を削除→IdP役ドメイン `jetuse-idp-test` を無効化→削除完了(GONE)。
jetuse-dev-domainのみ残存。検証で使ったマネージドのbuilding block手順は手順書付録に保存済み。
