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

## 実環境 E2E 証跡の添付（完了ゲート時）
Codex は **read-only sandbox でコードを実行できない**。完了ゲートでは、Claude が先に jetuse-dev へ
デプロイして複数シナリオ E2E を実施し、証跡を `runs/<run-id>/e2e/` に残す（手順は `loop-protocol`）。
`run_codex_review.sh` は diff に加えて `runs/<run-id>/e2e/` の内容を Codex 入力へ自動添付し、
`review-<n>.payload.txt` に保存する。Codex は「証跡が実環境で実際に動いた証拠になっているか」も採点する。

## レビュー観点（スクリプトのプロンプトに埋め込み済み）
- 正確性・境界条件・エラー処理・後方互換（公開シグネチャ）・テスト網羅。
- **実環境 E2E**: 添付された E2E 証跡が diff の主張を裏づけているか。複数シナリオ（最低2本）を
  網羅しているか。デプロイ/E2E 未実施なのに完了を主張していないか。未実施範囲は `e2e/SKIPPED.md`
  に正当な理由があるか。証跡が無い／不十分なまま完了を主張していれば指摘する。
- 本リポジトリ固有: 認証情報やテナンシ/コンパートメント OCID・エンドポイント実値を
  コミットしていないか（`.env` 管理の逸脱）。既存リソースを参照のみに留めているか
  （jetuse-dev への開発リソース作成は承認済み。IAM/テナンシ変更・既存リソース変更は人間ゲート）。
- 重大度を blocker / major / minor で必ず分類し、各指摘に `file:line` と修正案を付ける。
- 見逃しより過剰報告を許容するが、minor を blocker に格上げしない。
