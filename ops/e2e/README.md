# 公開スタックの実ブラウザ E2E ハーネス

デプロイ済みの JetUse に対して **Chromium で実際にログインして操作する** E2E。
FIX-58（公開ワンクリックデプロイの9件修正）の検証で作り、38項目 PASS を確認したもの。

Codex は read-only sandbox でコードを実行できないため、**Claude 側がこれを走らせて証跡を
`runs/<run-id>/e2e/` に残す**（`loop-config.yml` の `e2e:` / `.claude/skills/loop-protocol`）。

## 使い方

```bash
npm --prefix ops/e2e install
npx --prefix ops/e2e playwright install chromium     # 初回のみ

node ops/e2e/make-assets.mjs ops/e2e/assets/ocr.png  # OCR用テスト画像を生成（初回のみ）

APP_URL="https://<apigw>.apigateway.<region>.oci.customer-oci.com/" \
DEMO_USER=demo DEMO_PASS='<Stack出力の demo_password>' \
OUT_DIR=./shots ASSETS=ops/e2e/assets \
node ops/e2e/public-deploy.mjs
```

- 合否一覧は `$OUT_DIR/results.json`、スクリーンショットは `$OUT_DIR/*.png`
- 4xx/5xx レスポンスは全件 `$OUT_DIR/http-errors.txt` に記録される（**0件が合格条件**）
- 終了コードは全件 PASS で 0

## 証跡に残すときの注意

**実パスワード・OCID・実エンドポイントをコミットしない。** `results.json` の `detail` には
URL や OCID が混ざるため、`runs/<run-id>/e2e/` へ置く前にマスクする（FIX-58 では
apigateway URL と `ocid1.*` を正規表現で `<redacted>` に置換した）。

## 収録シナリオ（38項目）

| 群 | 内容 |
|---|---|
| 到達性 | ログイン（パスワード変更を強制されないこと）／SPA描画／`/api/health` 全体ok／capability 6種／`/api/rag/health` 3点 |
| チャット | 既定モデル応答・フォールバック通知が出ないこと・他4モデル・会話メモリ作成 |
| RAG | UIから文書アップロード（504にならないこと）→索引化→**引用付きで正答** |
| DBチャット | 「2001年の販売チャネル別売上」→SQL生成→実行→結果テーブル |
| マルチモーダル/音声 | OCR（画像→文字）／議事録の音声登録／リアルタイムSTTセッション／TTS合成＋health反映／翻訳 |
| その他 | エージェント一覧／管理ダッシュボード（demoユーザーで200）／全ページ描画 |

## ハマりどころ（FIX-58 実機）

- **Identity Domain のログインフォーム**: ユーザー名は `#idcs-signin-basic-signin-form-username`、
  パスワードは `input[type=password]`、送信は `getByRole('button', {name: 'Sign In'})`。
- **DBチャットは Enter では送信されない**。「SQL生成」ボタンのクリックが要る。
  さらに合否判定は**生成SQL欄の入力値**を見ること — 画面テキストで `SELECT` を探すと
  注意書き「SELECT文のみ・最大200行」に一致して**誤ってPASSする**。
- **OCR もファイル選択後に「OCR実行」ボタン**のクリックが要る。
- **RAG は索引化まで待つ**。`/api/rag/files` が `status=completed` かつ
  `backends.vector_store=indexed` になってから質問する。
- デプロイ直後はサンプルデータ投入や project 伝播が終わっていないことがある。
  apply 直後は数分待ってから流す。
