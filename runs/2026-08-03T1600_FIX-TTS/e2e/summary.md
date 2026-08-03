# 不具合を検出した受け入れ E2E（修正前・配備済みスタック）

対象: Public版を `jetuse:test` / us-chicago-1 へ配備した環境（画像は main@1df8c90 = AGT-06）。
実行: `ops/e2e/public-deploy.mjs`（39項目）と `ops/e2e/agents-3sdk.mjs`（9項目）。

| スイート | 結果 |
|---|---|
| public-deploy.mjs | **38 / 39 PASS** |
| agents-3sdk.mjs | **9 / 9 PASS** |

4xx/5xx レスポンスは全体で **1件のみ**（`before-fix_http-errors.txt`）:

```
503 POST /api/tts
```

## 唯一の不合格

```
FAIL  TTS: 音声合成 — 503 {"detail": "音声合成に失敗しました: InternalError Internal Server Error"}
PASS  TTS: health が実合成の結果を反映(verified)
      — {"status":"ok","region":"us-phoenix-1","candidate_regions":["us-phoenix-1","us-chicago-1"],"verified":true}
```

**health は緑なのに合成だけ 503** という食い違いが、原因究明の入口になった（`region-probe.md`）。
2回連続で同じ結果になり、再現性のある不具合であることを確認している。

合格した群: ログイン / 全ページ描画 / チャット（既定＋他4モデル）/ 会話メモリ /
RAG（アップロード→索引→引用付き回答）/ DBチャット（SQL生成→実行）/ OCR / 議事録 /
リアルタイムSTT / 翻訳 / 管理ダッシュボード / エージェント3種。

注: 配備直後の1回目の実行はログインが完了せず途中終了した（以降が 401）。同じログイン処理を使う
エージェント側は直後に 9/9 成功しており、再実行で最後まで通ったため初回ログイン特有の
タイミング問題と判断した。恒常的な不具合ではない。

生ログは `*.log` が `.gitignore` 対象のため未コミット（本要約と `before-fix_http-errors.txt` が代替）。
