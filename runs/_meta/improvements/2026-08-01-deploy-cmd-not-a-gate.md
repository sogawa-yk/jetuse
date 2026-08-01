# 2026-08-01 deploy_cmd 等に定義された操作は人間ゲートではない、と明文化

- 対象 run: `runs/<PREP-03 の run-id>/`（review-1〜10 が同一 blocker で空転）
- 対象ファイル: `loop-config.yml` / `.claude/skills/codex-review/scripts/run_codex_review.sh`
- 承認: 2026-08-01 ユーザー承認

## 症状

実環境 E2E を伴うタスクが、**共有 ADB を起動しただけで blocker** になり進めない。
PREP-03 は実装が完了し 2〜10 ラウンドで実装上の欠陥をすべて解消したのに、
**10 ラウンド一貫して同じ blocker** のまま停止した。

## 証跡

- `run_codex_review.sh:63` のレビュアー指示に
  「**既存リソース変更は人間ゲート**」とあり、Codex はこれに従って
  `ops/start-adb-if-stopped.sh` による ADB 起動を「承認証跡の無い状態変更」と判定した。
- 一方 `loop-config.yml` の `areas.api.deploy_cmd` は
  `ops/start-adb-if-stopped.sh && .venv/bin/python -m jetuse_core.migrate` と定義されており、
  **設定自身が起動を指示している**。複数タスクの「前提」や `docs/guides/demo-scenarios.md` にも
  通常フローとして記載がある。
- つまりループは指示どおり動いただけで、独断で共有リソースを触ってはいなかった。
  **レビュアー指示と実行設定が矛盾していた**のが原因。

## 変更

- `loop-config.yml`: `deploy_cmd` / `test_cmd` / `lint_cmd` / `e2e_cmd` に定義された操作は
  人間ゲートではない、と明記。人間ゲートは `stage_runner.hard_gates`（push / PR / apply /
  課金 / IAM / ADR 承認）とコミットに限る。**そこに無い操作を新たにゲート扱いしない**。
- `run_codex_review.sh`: レビュアー指示から「既存リソース変更は人間ゲート」を外し、
  上記の除外と、それでもゲートである操作（deploy_cmd 等に書かれていない DROP / destroy /
  削除、コミット・push・PR・apply・課金・IAM）を明示した。

## 副作用

レビューが「共有リソースの状態変更」を見なくなる。**deploy_cmd 等に書かれていない**破壊的操作は
引き続きゲートとして残るので、無制限ではない。とはいえレビューの網が 1 つ緩むのは事実なので、
破壊操作の判定は `spikes/*/teardown.py` 側の所有権照合（run 固有マーカー）に依存が寄る。

## 検証

次に実環境 E2E を伴うタスクを回したとき、ADB 起動が blocker にならないことで判断する。

## ループが正しかった点（記録）

Codex の指摘は厳しすぎたが、**ループが自分で判定を覆さず人間ゲートへ上げたのは正しい振る舞い**。
「採点者の判定を実装者が書き換えない」という原則は守られていた。直すべきはループの振る舞いではなく、
矛盾していた設定側だった。
