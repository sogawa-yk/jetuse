# INFRA-02c 検証レポート: 放置試験不具合（5秒周期の画面フラッピング）の修正

日付: 2026-06-11
仕様: specs/06-oidc.md「トークン更新v2（INFRA-02c）」
状態: **修正デプロイ済み**（放置再試験はユーザー確認待ち）

## 報告された症状（放置試験の結果）

約5秒に1回「読み込み中画面⇔トップページ」の切り替わりが続き、しばらくして再ログイン画面に到達。

## 確定した事実（コード・実機）

1. **oidc-client-tsはsilent renewのiframeタイムアウト時、`maxSilentRenewTimeoutRetries`未設定（既定）だと無制限に5秒間隔でリトライする**（node_modules内ソースで確認: `ErrorTimeout from signinSilent, retry in 5s` → `_retryTimer.init(5)`、上限チェックは設定がある場合のみ）
2. INFRA-02bで入れた expired/renewError→`signinRedirect()` の自動リダイレクトには**ループ保護がなかった**
3. IDCSの`prompt=none`はセッションなしで302 `?error=login_required` を返す（curl実測）。iframe方式はSameSite Cookie送信可否・state共有・タイムアウトに依存し、ブラウザ環境で挙動が変わる

## 修正（task/auth-fix-2、iframe方式の全廃）

- `automaticSilentRenew: false` — 隠しiframe・5秒リトライの機構自体を使わない
- `accessTokenExpiring`（期限60秒前）でトップレベル`signinRedirect()`を**単発**実行。Domainセッション生存中は無操作で復帰（第三者Cookie遮断の影響も受けない）
- **30秒ガード**: 自動signinRedirectは30秒に1回まで（sessionStorage記録）。超過時は自動遷移せず「セッションの有効期限が切れました［再ログイン］」画面（手動ボタン）を表示 — **ループが構造的に起きない**
- `?error=`リダイレクト・API 401時の`reauthenticate()`も同じガード経由

## 検証結果

- [x] lint / build クリーン、SPAデプロイ済み
- [x] ループ上限の構造保証（30秒ガード）はコードレビューで確認（自動遷移経路はすべてguardedReauth経由）
- [ ] **ユーザー確認待ち（放置再試験）**: 期待挙動 = 放置中は約1時間ごとに一瞬画面が再読み込み（セッション生存中）→ Domainセッションが切れた後は「再ログイン」ボタン画面または IDCSログイン画面で**静止**（フラッピングしない）

## 既知の制約

- 自動再ログイン時にページ遷移が走るため、未送信の入力テキストは失われる（会話はADB保存済み）。改善するならrefresh token方式（Domain設定変更=人間承認）
- 再ログイン後はトップページに戻る（HashRouterのルートはredirect時に保持していない）。頻度が低いため現状許容、CP②で要否判断
