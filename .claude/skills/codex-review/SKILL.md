---
name: codex-review
description: 直近の差分を Codex にレビューさせ、判定(PASS/FAIL)と指摘を構造化して記録する。コードを実装・修正した直後は必ずこのスキルを使うこと。レビューを飛ばして次に進んではならない。採点者は Codex であり、判定を Claude が書き換えてはならない。
---
# Codex レビューの実行

実装者（Claude）とは別ツール・別モデルである Codex に、いま作った差分をレビューさせる。
maker/checker をツールをまたいで分離するための中核スキル。

## 手順
1. レビュー対象の差分範囲を決める（既定: `loop-config.yml` の `diff_scope` = uncommitted）。
2. `scripts/run_codex_review.sh` を実行する。スクリプトは以下を行う:
   - `git rev-parse` でリポジトリルートへ移動し、`.current_run_id` から run-id を取得。
   - 差分を Codex に渡し、`--output-schema scripts/review-schema.json` で構造化 JSON を直接得る。
   - 構造化結果を `runs/<run-id>/reviews/review-<n>.json` に、生イベントを `review-<n>.raw.txt` に保存。
   - 標準出力の最終行に `VERDICT: PASS|FAIL` とJSONパスを表示する。
3. スクリプトが書いた `review-<n>.json` を読み、その verdict / severity_counts / findings を確認する。
   blocker が1件でもあれば verdict は FAIL（スキーマ側で強制。手で緩めない）。
4. STATE.md の `review_verdict` と `last_review_ref` を、いま得た結果で更新する。

> スキーマで JSON を直接生成させているため、生出力からの手動抽出は不要。
> 抽出に失敗した／JSON が空のときだけ `review-<n>.raw.txt` を読んで原因を確認する。

## レビュー観点（スクリプトのプロンプトに埋め込み済み）
- 正確性・境界条件・エラー処理・後方互換（公開シグネチャ）・テスト網羅。
- 本リポジトリ固有: 認証情報やテナンシ/コンパートメント OCID・エンドポイント実値を
  コミットしていないか（`.env` 管理の逸脱）。既存リソースを参照のみに留めているか。
- 重大度を blocker / major / minor で必ず分類し、各指摘に `file:line` と修正案を付ける。
- 見逃しより過剰報告を許容するが、minor を blocker に格上げしない。
