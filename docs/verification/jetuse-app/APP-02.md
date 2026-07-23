# APP-02 検証レポート: React SPA骨格（OCIコンソール風）

日付: 2026-06-10
仕様: specs/05-app-web.md
状態: **実装完了・実環境配信済み。ルック&フィールは人間レビュー待ち**

## 実行結果

| チェック | 結果 |
|---|---|
| `npm run lint` / `npm run build` | クリーン（249KB JS / 19KB CSS gzip前） |
| preview (port 4173) | 200応答（ルック確認用に起動中） |
| 実環境配信 | `jetuse-dev-spa` へデプロイ → **API GW経由で `/` 200 text/html、branding.json 200**。タイトル「GenAI Use Cases on OCI」描画確認 |
| ハッシュルーティング | `#/chat` 等はクライアント側解決のためサーバーリクエストが発生せず、**ディープリンク404問題は構造的に発生しない**（ADR-0004 検証3の結論として採用） |

## 実装内容

- **OCIコンソール風シェル**: ダークヘッダ（#161513・ハンバーガー・ブランドマーク・言語/テーマ切替・ユーザーチップ）+ 白い左ナビドロワー（選択状態はアクション色の左バー）+ ライトグレー本文/白カード
- **トークン拡張**（`theme.css`）: header / action（ティールブルー）/ cta（ほぼ黒のプライマリボタン）を追加。Oracle Red系はブランドマークとdangerに限定。branding.jsonリブランドとダークモード反転は維持
- **ページ**: ホーム（ユースケースカード。未実装分は「近日対応」のプレースホルダ）/ チャット（空状態+入力欄+モデル選択。送信はCHAT-03で接続）/ 設定（テーマ・言語・ブランド切替）/ デザインギャラリー（SPIKE-07温存）
- **i18n雛形**（ja/en、localStorage永続）/ **認証スタブ**（`VITE_AUTH_REQUIRED` フラグ。INFRA-02でPKCE実装に差替）

## 確認方法（人間レビュー）

- **インターネットから直接**: `https://<apigw_hostname>/`（`terraform output apigw_hostname`。実値は .env 管理者に共有済みのGWホスト名）
- またはSSHトンネル: `ssh -L 4173:localhost:4173 opc@<devインスタンス>` → http://localhost:4173

## 残課題

- [ ] ルック&フィールの承認（→ 修正フィードバック反映）
- [ ] OIDCログインフロー（INFRA-02後にauth.tsxを実装差替、E2E）
- [ ] SPAデプロイスクリプト化（content-type付きアップロードの定型化。CI-02相当で自動化）
