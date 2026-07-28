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
   blocker が1件でもあれば verdict は FAIL（スキーマ側で強制）。判定は Codex のものであり、
   Claude は結果を STATE.md へ転記するだけ（見せ方は整えてよいが、verdict は動かさない）。
4. STATE.md の `review_verdict` と `last_review_ref` を、いま得た結果で更新する。
5. **findings をタスクパケットへ渡す（露出は例外だけ）。** 完了ゲートで作る HTML タスクパケット
   （`loop-protocol` 参照）への転記ルール:
   - **clean PASS（例外なし）**: パケットには**判定1行のみ**（`PASS (review-N) / blocker0 major0`）。
     全 findings は `<details class="aud">` 監査用テーブルに畳む＝人間の必読ではない。バナーは出さない。
   - **FAIL を override して統合する / 未対応 residual を残す**ときだけ、その決定に関わる**具体 findings
     （id・severity・file:line・issue）と理由**をパケットの「判断が要る事項」バナーへ inline する。
     これが唯一「人間が Codex 指摘を読む」ケース（SP3-03 の 18×FAIL override が表の1セルに埋もれた失敗の是正）。

> スキーマで JSON を直接生成させているため、生出力からの手動抽出は不要。
> 抽出に失敗した／JSON が空のときだけ `review-<n>.raw.txt` を読んで原因を確認する。

## 実環境 E2E 証跡の添付（完了ゲート時）
Codex は **read-only sandbox でコードを（シェルとして）実行できない**。完了ゲートでは、Claude が先に
jetuse-dev へデプロイして複数シナリオ E2E を実施し、証跡を `runs/<run-id>/e2e/` に残す（手順は
`loop-protocol`）。`run_codex_review.sh` は diff に加えて `runs/<run-id>/e2e/` の内容を Codex 入力へ
自動添付し、`review-<n>.payload.txt` に保存する。Codex は「証跡が実環境で実際に動いた証拠になっているか」
も採点する。

## ライブ E2E（Codex が Playwright MCP で実ブラウザ検証）
Codex は **Playwright MCP の browser ツール**（`browser_navigate` / `browser_snapshot` /
`browser_evaluate` / `browser_click` 等）を使える。到達可能なターゲット URL が与えられたときだけ、
Codex 自身が実ブラウザで diff 関連の主要フローを**独立検証**する（read-only sandbox 下でも MCP ツールは
動く。シェル実行とは別系統）。

- **URL の渡し方**（どちらか。env が優先）:
  - env `E2E_BASE_URL=https://...`、または
  - ファイル `runs/<run-id>/e2e/target_url.txt` の先頭の非コメント行に URL を1行。
  → maker（Claude）が完了ゲートで jetuse-dev にデプロイした後、その公開 URL をここに書くと
     Codex がレビュー時に実ブラウザで確認する。**URL 未提供時は従来どおりブラウザは使わず証跡評価のみ。**
- **判定への影響**: 主要フローが実ブラウザで壊れていれば Codex は severity=blocker を立て、verdict は FAIL。
- **記録先**: `review-<n>.json` の `e2e.live_check`（performed / target_url / result / scenarios / notes）。
  ブラウザで確認できた挙動は、添付された静的証跡より優先して評価される。

> 注: jetuse-dev の公開 LB は自 IP 限定のため、この dev インスタンスから到達できる URL を渡すこと。
> 公開インターネット一般（例: example.com）は egress が制限され得る。

## レビュー観点
Codex に渡す評価観点（正確性・E2E 証跡の十分性・本リポジトリ固有の秘匿値/リソース規約・
severity 分類）の正本は `scripts/run_codex_review.sh` の `INSTRUCTIONS` 変数。観点を変えるときは
そこ（と `scripts/review-schema.json`）を loop-doctor 経由で編集する。本ファイルには重複記載しない。
