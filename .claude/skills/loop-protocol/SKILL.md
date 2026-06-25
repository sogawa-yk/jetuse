---
name: loop-protocol
description: /goal ループの毎ターンの手順。実装→Codexレビュー→履歴記録→状態更新を必ずこの順で行う。コードを実装・修正するたびに必ず参照すること。レビューや履歴記録の省略は禁止。
---
# ループ手順（毎ターン厳守）

ループの各ターンで Claude（実装者）は次を厳守する。採点者は Codex であり、自分ではない。

1. **STATE.md を読む。** `review_verdict` と未完タスクを確認し、未完から1つだけ選ぶ。
   着手前に直近 run の `runs/<run-id>/reviews/` を見て、同じ指摘の再発でないか確認する。
2. **最小の差分で実装する。** 1ターンで広げすぎない。受け入れ条件の1項目に集中する。
3. **codex-review スキルを起動**し、いま作った差分を Codex にレビューさせる。
   レビューを飛ばして次のターンに進んではならない。
4. **履歴と状態を更新する。**
   - codex-review が `runs/<run-id>/reviews/review-<n>.json`（+ `.raw.txt`）を生成する。
   - STATE.md の `review_verdict` / `last_review_ref` / 未完リスト / 指摘要約 / `updated_at` を更新する。
5. **FAIL の指摘は次ターンで修正する。** `review_verdict` を自分で PASS に書き換えてはならない。
   PASS になるのは Codex レビューが blocker ゼロを返したときだけ。
6. **コミット / PR / push は行わない**（人間ゲート。CLAUDE.md「やってはいけないこと」）。

## /goal 完了条件との関係
`/goal` の完了判定（別モデル）は、STATE.md の `review_verdict == PASS` かつ
該当 area のテスト・lint がクリーンであることを真偽として読む。だから実装者が
「できた」と思っても、Codex が PASS を出すまでループは止まらない。これが maker/checker 分離の要。

## なぜこの順番か
採点を実装者と分けることで「完了」が主張ではなく証明に近づく。履歴を残すのは、
後で loop-doctor がこの仕組み自体を改善できるようにするため（runs/ が唯一の根拠資料）。
