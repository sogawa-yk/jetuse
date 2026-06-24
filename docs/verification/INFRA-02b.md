# INFRA-02b 検証レポート: トークン更新不具合の修正

日付: 2026-06-10
仕様: specs/06-oidc.md「トークン更新（INFRA-02b）」
状態: **修正デプロイ済み**（1時間放置の実機再現確認はユーザー確認待ち）

## 報告された症状

1. メッセージを送信しても何も反応しない
2. 画面を開いて放置するとしばらくして真っ白になる（リロードで復旧）

## 切り分け（実機）

- Container Instance: ACTIVE。M2Mトークン（client_credentials）取得 → API GW経由で `/api/chat/models` 200、`/api/chat/stream` SSE完走（gpt-oss、usage含む）→ **バックエンドは正常**
- デプロイ済みバンドル: 認証ON・正しいDomain設定が焼き込まれていることを確認（ビルド環境変数の混入なし）
- rehype-highlight（CHAT-03b）: 未知言語のコードブロックでも例外を投げないことをNodeで実測 → 白画面の原因ではない

## 根本原因（フロントの認証実装）

- `auth.tsx` がアクセストークンを**起動時に1回だけ**取得し、React状態を以後更新していなかった
- oidc-client-ts は既定 `automaticSilentRenew=true` で、期限60秒前に隠しiframeで更新を試みる。専用silent redirect URIが無いため **`redirect_uri`（=SPAルート）がiframeに読み込まれ、SPA全体がiframe内で再起動**していた（状態破壊・白画面の原因）
- トークンTTL（約1時間）経過後はReact側の古いトークンで全APIが401 →「送信しても無反応」。リロードで取り直すため復旧する、と症状が完全に一致

## 修正内容（task/auth-fix-1）

- iframe内（`window.self !== window.top`）では `signinSilentCallback()` のみ実行しアプリを起動しない（既存redirect URI流用のため**Identity Domain設定変更は不要** = 人間承認事項を回避）
- `events.addUserLoaded` でReactのトークンを更新追随
- `addSilentRenewError` / `addAccessTokenExpired` → `signinRedirect()`（Domainセッション残存時は無操作で復帰。サードパーティCookie遮断ブラウザのフォールバック兼用）
- boot時に `?error=`（prompt=none失敗等の認可エラーリダイレクト）を検出して通常ログインへ
- chat.tsx: `/api/chat/stream` が401の場合 `reauthenticate()` で再ログイン（メッセージ表示後にリダイレクト）

## 検証結果

- [x] lint / build クリーン → `scripts/deploy.sh` でデプロイ、新バンドル（index-Cry6KZiI.js）配信確認
- [x] バックエンドE2E正常（上記切り分け）
- [ ] **ユーザー確認待ち**: ログイン後1時間以上放置 → 白画面にならない / 送信が通る（または一瞬「再ログインします…」表示後に自動復帰する）こと

## 備考

- 401からの `signinRedirect` はページ遷移を伴うため、送信途中の入力テキストは失われる（会話履歴はADB永続化済みのため復元される）。頻発するようならrefresh token方式（`offline_access`、Domain側設定要変更=人間承認）を検討
