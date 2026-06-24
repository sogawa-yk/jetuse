# CHAT-03b 検証レポート: コードハイライト + Mermaidレンダリング

日付: 2026-06-10
仕様: specs/07-chat.md [CHAT-03b]
状態: **実装・デプロイ完了**（UI見た目の最終確認はチェックポイント②のデモで実施）

## 実装

- `components/markdown.tsx` に `Md` コンポーネントを新設し、chat.tsx のアシスタント応答レンダリングを差し替え
- シンタックスハイライト: rehype-highlight（highlight.js共通サブセット、`detect: false`=言語指定があるブロックのみ）。テーマは `github-dark` を `.md pre` の黒背景に透過で重ねる
- Mermaid: ` ```mermaid ` ブロックを `MermaidBlock` でSVG描画
  - mermaid本体は **動的import**（初期バンドル非含有を実測確認 — `index-*.js` にダイアグラムコードなし、mermaid系は約40チャンクに分離）
  - ストリーミング中の未完成ソース対策: 200msデバウンス + `mermaid.parse(suppressErrors)` が通った時のみ `render`。それまでは通常のコードブロック表示（エラーフラッシュなし）
  - ダークモード追随（`theme: dark/default`、`usePrefs().dark` 変化で再描画）。`securityLevel: 'strict'`
- デプロイ定型化（バックログ#8の前半）: `packages/web/scripts/deploy.sh` — dist/ を content-type / cache-control 付きで `jetuse-dev-spa` へアップロード（ハッシュ付きassetsは `immutable`、index.html等は `no-cache`）

## 検証結果

- [x] lint / `npm run build`（tsc -b + vite build）クリーン
- [x] mermaidが初期バンドルに含まれない（`grep sequenceDiagram dist/assets/index-*.js` → 0件、動的import 2箇所）
- [x] 実機: `scripts/deploy.sh` で86オブジェクトをデプロイ → API GW経由で配信確認
  - `/` → 200 `text/html;charset=utf-8`（新ハッシュの index-C77ohTrz.js / index-Dk8U1GBp.css を参照）
  - `/assets/index-C77ohTrz.js` → 200 `text/javascript`
  - `/assets/sequenceDiagram-*.js`（mermaid遅延チャンク）→ 200 `text/javascript`
- [ ] ブラウザでのハイライト表示・mermaid SVG描画・ダークモードの目視確認 → **チェックポイント②のユーザーレビューで実施**（curlでは確認不能のため）

## トラブル記録

- 前回セッションが夜間インスタンス停止で `npm install` 展開中に強制終了し、**node_modules内の113パッケージが0バイトファイルとして残存**（npmは存在扱いで再installでも修復されない）。`rm -rf node_modules && npm cache verify && npm ci` で復旧。

## 残課題

- 旧アセット（index-BcIvpsAJ.js 等）はバケットに残置（削除はスパイクプレフィックス外のため人間承認事項。実害なし）
- デプロイのCI化（CI-02相当、バックログ#8後半）
