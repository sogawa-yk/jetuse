---
name: loop-protocol
description: /goal ループの毎ターンの手順。実装→Codexレビュー→履歴記録→状態更新を必ずこの順で行う。コードを実装・修正するたびに必ず参照すること。レビューや履歴記録の省略は禁止。
---
# ループ手順（毎ターン厳守）

ループの各ターンで Claude（実装者）は次を厳守する。採点者は Codex であり、自分ではない。

**2層構成（合成・C案 / 施主承認 2026-06-30）**: 開発者の規律は **Superpowers（obra/Superpowers）を無改造で
install して invoke** し借りる（TDD / brainstorming / writing-plans / systematic-debugging 等）。このループ自身は
**採点(Codex)・実環境 E2E ゲート・fail-closed ハードゲート・loop-doctor の「強制と運用の殻」** を担う。
Superpowers は「どう作るか」を導き、このループのゲートが「**完了か否か**」を決める。**フォークしない＝upstream を
無税で追従**する（`/plugin update`）。どのスキルを使うかは `loop-config.yml` の `superpowers:` を単一真実源とする。

1. **STATE.md を読む。** `review_verdict` と未完タスクを確認し、未完から1つだけ選ぶ。
   着手前に直近 run の `runs/<run-id>/reviews/` を見て、同じ指摘の再発でないか確認する。
2. **最小の差分で実装する。** 1ターンで広げすぎない。受け入れ条件の1項目に集中する。
   実装・リファクタ時は `ponytail:ponytail` スキル（既定強度は `loop-config.yml` の `ponytail.intensity`）を適用し、
   はしご（YAGNI→既存資産の再利用→stdlib→ネイティブ機能→既存依存→1行→最小コード）で**最短で効く実装**を選ぶ。
   ただし ponytail の「When NOT to be lazy」どおり、**信頼境界の入力検証・データ損失を防ぐエラー処理・
   セキュリティ（broker fail-closed / 署名検証 / Vault）・明示要求事項は簡略化しない**。本リポジトリの
   ADR / 比較ドキュメント / 検証レポート / spec は CLAUDE.md が明示要求する成果物であり、ponytail の
   「不要散文の削減」の対象外（最小化するのはコードであって、必須ドキュメント規律ではない）。
   完了ゲートの実環境 E2E（`e2e.min_scenarios` ≥2）は ponytail の「one runnable check」より優先＝弱めない。
   非自明なロジック（分岐 / ループ / パーサ / 金銭・セキュリティ経路）は Superpowers の
   `superpowers:test-driven-development` を踏む＝**RED（失敗するテスト先行）→ GREEN（最小実装）→ REFACTOR**。
   これは実環境 E2E 証跡（完了ゲート）と相補で、ユニット段の退行を捕まえる。要件が曖昧なら着手前に
   `superpowers:brainstorming`、多段なら `superpowers:writing-plans`、不具合解析は `superpowers:systematic-debugging` を reach する
   （`loop-config.yml` の `superpowers.on_demand_skills`）。ponytail（最小化）と TDD（先にテスト）は
   矛盾しない＝「最小だが検証付き」に倒す。
2.5. **（推奨）over-engineering 自己レビュー。** `ponytail.self_review: true` のとき、codex-review の**前に**
   `ponytail:ponytail-review` を差分に当て、削れる複雑性（再発明 stdlib・不要依存・投機的抽象・死んだ柔軟性）を
   **maker 自身が短く**する。出力は `runs/<run-id>/reviews/ponytail-<n>.txt` に証跡として残す。
   **これは採点ではない。** `review_verdict` を動かせるのは Codex だけ（§5）。ponytail-review はあくまで
   Codex に渡す前に差分を短くするための maker 側規律であり、Codex の正確性採点に混ぜてはならない。
3. **codex-review スキルを起動**し、いま作った差分を Codex にレビューさせる。
   レビューを飛ばして次のターンに進んではならない。
4. **履歴と状態を更新する。**
   - codex-review が `runs/<run-id>/reviews/review-<n>.json`（+ `.raw.txt`）を生成する。
   - STATE.md の `review_verdict` / `last_review_ref` / 未完リスト / 指摘要約 / `updated_at` を更新する。
5. **FAIL の指摘は次ターンで修正する。** `review_verdict` を自分で PASS に書き換えてはならない。
   PASS になるのは Codex レビューが blocker ゼロを返したときだけ。
5.5. **PASS に達したら磨き込みで空回りしない（停止規律）。** `review_verdict=PASS`（＝blocker ゼロ）かつ
   area の test/lint 緑かつ実環境 E2E 済になった時点で**完了ゲートは満たされている**。ここで**停止する**。
   PASS の下に残る **major / minor は非 blocker の助言**であり、それらを潰すために**さらにコードを変えて新規
   codex-review を回してはならない**（各修正が新たな助言を生み、PASS のまま反復が止まらず＝トークン浪費。
   実例: EXB-03 が PASS 後も major を追って計12ラウンド・約2h/220kトークン）。残る非 blocker 指摘は**修正せず**、
   STATE.md と最終メッセージに **residual（後続/人間トリアージ）** として file:line 付きで列挙する。
   PASS 到達後に更にコードを変えてよいのは、(a) その変更が**受け入れ条件の未達**を埋める場合、または
   (b) **人間/オーケストレータが明示指示**した場合のみ。blocker（FAIL）は従来どおり次ターンで必ず修正する。
6. **コミット / PR / push は行わない**（人間ゲート。CLAUDE.md「やってはいけないこと」）。

## 完了ゲート：デプロイ＋実環境 E2E（毎イテレーションではなく1回）
受け入れ条件を満たし静的 Codex レビューが PASS になったと判断したら、**最終 PASS を主張する前に**
実環境 E2E を1回だけ実施する（`loop-config.yml` の `e2e` ブロックと該当 area の `deploy_cmd`/`e2e_cmd`）。
Codex はコードを実行できない（read-only）。だから **Claude がデプロイと E2E を実施し、証跡を残す**。

1. **デプロイ**: 該当 area の `deploy_cmd` で jetuse-dev の**固定 loop 環境を再利用**してデプロイする
   （むやみにリソースを増やさない。作り直す場合は Terraform で破棄→再作成。[[jetuse-dev-terraform-resources-ok]]）。
   出力を `runs/<run-id>/e2e/deploy.log` に保存する。
2. **複数シナリオ E2E**: `tasks/<id>.md` の「E2E シナリオ」を**最低 `e2e.min_scenarios`（既定2）本**実行し、
   各シナリオの実行コマンド・期待結果・実結果（HTTP応答 / DB状態 / スクショ等）を
   `runs/<run-id>/e2e/scenario-<n>.*` に証跡として残す。
3. **ベストエフォート＋無言スキップ禁止**: タスク特性で E2E 不能・限定的な範囲は、
   `runs/<run-id>/e2e/SKIPPED.md` に「何を・なぜ実施できないか」を明記する（理由なしの省略は禁止）。
4. **証跡込みレビュー**: その後に codex-review を起動する。`run_codex_review.sh` は diff に加えて
   `runs/<run-id>/e2e/` の証跡を Codex 入力に添付する。Codex は証跡の十分性（複数シナリオ網羅・
   未実施の正当性・実環境で実際に動いた証拠）も含めて採点する。
5. **人間ゲート**: jetuse-dev へのデプロイ（Terraform apply 含む）は承認済み。ただし IAM/テナンシ変更、
   既存リソース（VCN develop / インスタンス dev / バケット）変更、コミット/PR/push は引き続き人間ゲート。

## /goal 完了条件との関係
`/goal` の完了判定（別モデル）は、STATE.md の `review_verdict == PASS` かつ
該当 area のテスト・lint がクリーンであることを真偽として読む。だから実装者が
「できた」と思っても、Codex が PASS を出すまでループは止まらない。これが maker/checker 分離の要。

## なぜこの順番か
採点を実装者と分けることで「完了」が主張ではなく証明に近づく。履歴を残すのは、
後で loop-doctor がこの仕組み自体を改善できるようにするため（runs/ が唯一の根拠資料）。
