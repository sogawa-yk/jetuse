---
name: loop-protocol
description: goal ループの毎ターンの手順。実装→Codexレビュー→履歴記録→状態更新をこの順で行う。コードを実装・修正するたびに必ず参照すること。レビューと履歴記録は毎ターン実施する。
---
# ループ手順（毎ターン）

ループの各ターンで Claude（実装者＝maker）は次の 1→7 を順に行う。採点者（checker）は Codex であり、自分ではない。

**2層構成（合成・C案）**: 開発者の規律は Superpowers（obra/Superpowers）を無改造 install して invoke し借りる。このループ自身は採点（Codex）・実環境 E2E ゲート・fail-closed ハードゲート・loop-doctor の「強制と運用の殻」を担う。Superpowers が「どう作るか」を導き、このループのゲートが「完了か否か」を決める。フォークせず upstream を `/plugin update` で追従する。使うスキルは `loop-config.yml` の `superpowers:` が単一真実源。

1. **STATE.md を読む。** `review_verdict` と未完タスクを確認し、未完から1つだけ選ぶ。
   着手前に直近 run の `runs/<run-id>/reviews/` を見て、同じ指摘の再発でないか確認する。
2. **最小の差分で実装する。** 1ターンで広げすぎず、受け入れ条件の1項目に集中する。
   進め方は下記「実装規律」に従う。
3. **codex-review スキルを起動**し、いま作った差分を Codex にレビューさせてから次へ進む。
4. **履歴と状態を更新する。**
   - codex-review が `runs/<run-id>/reviews/review-<n>.json`（+ `.raw.txt`）を生成する。
   - STATE.md の `review_verdict` / `last_review_ref` / 未完リスト / 指摘要約 / `updated_at` を更新する。
   - `review_verdict` は Codex の結果の転記であり、PASS になるのは Codex が blocker ゼロを
     返したときだけ。判定は Codex のもの（maker/checker 分離の要）。
5. **FAIL の指摘は次ターンで修正する。**
6. **PASS に達したら停止する（停止規律）。** `review_verdict=PASS` かつ area の test/lint 緑
   かつ実環境 E2E 済で、完了ゲートは満たされている。求められたものを、意図された範囲で
   仕上げたら、そこで止める。PASS の下に残る major / minor は非 blocker の助言であり、
   修正せず STATE.md と最終メッセージの `residual` に
   （後続/人間トリアージ）として file:line 付きで列挙する。磨き込みの反復は各修正が新たな
   助言を生んで止まらなくなる（実例: EXB-03 が PASS 後に12ラウンド・約220kトークン）。
   PASS 後にコードを変えるのは (a) 受け入れ条件の未達を埋める場合、
   (b) 人間/オーケストレータの明示指示がある場合のみ。
7. **人間ゲートで停止する。** コミット / PR / push は人間の承認後（CLAUDE.md）。
   停止時は**構造化した事実**（下記「完了ゲートで人間へ返すもの」）を返す。**HTML は作らない**。

## 実装規律（手順2の中身）
- `ponytail:ponytail` スキル（既定強度は `loop-config.yml` の `ponytail.intensity`）を適用し、
  はしご（YAGNI→既存資産の再利用→stdlib→ネイティブ機能→既存依存→1行→最小コード）から
  最短で効く実装を選ぶ。ponytail の「When NOT to be lazy」どおり、信頼境界の入力検証・
  データ損失を防ぐエラー処理・セキュリティ（broker fail-closed / 署名検証 / Vault）・
  明示要求事項は完全に実装する。ADR / 比較ドキュメント / 検証レポート / spec は CLAUDE.md が
  要求する成果物なので最小化の対象外（最小化するのはコード）。完了ゲートの実環境 E2E は
  ponytail の「one runnable check」より優先する。
- 非自明なロジック（分岐 / ループ / パーサ / 金銭・セキュリティ経路）は
  `superpowers:test-driven-development`（RED→GREEN→REFACTOR）で書く。実環境 E2E 証跡と相補で、
  ユニット段の退行を捕まえる。
- 要件が曖昧なら着手前に `superpowers:brainstorming`、多段なら `superpowers:writing-plans`、
  不具合解析は `superpowers:systematic-debugging`（`loop-config.yml` の `superpowers.on_demand_skills`）。
- ponytail（最小化）と TDD（先にテスト）は「最小だが検証付き」に倒すことで両立する。

## ターン出力の書き方（無人ループ向け）
- 最初のツール実行前に、これから何をするかを1文で述べる。作業中の更新は重要な発見や
  方針転換があったときだけ短く。ターンの締めは「何が起きたか」を先頭の1文で示す。
- ディスクに書く成果物（STATE.md・SKIPPED.md・レポート類）はタスクに必要な長さに合わせる。
  実質を網羅し、埋め草セクション・冗長な要約・定型文で水増ししない。

## 完了ゲート：デプロイ＋実環境 E2E（毎イテレーションではなく1回）
受け入れ条件を満たし静的 Codex レビューが PASS になったと判断したら、最終 PASS を主張する前に
実環境 E2E を1回だけ実施する（`loop-config.yml` の `e2e` ブロックと該当 area の `deploy_cmd`/`e2e_cmd`）。
Codex はコードを実行できない（read-only）。だから Claude がデプロイと E2E を実施し、証跡を残す。

1. **デプロイ**: 該当 area の `deploy_cmd` で jetuse-dev の固定 loop 環境を再利用してデプロイする
   （むやみにリソースを増やさない。作り直す場合は Terraform で破棄→再作成。[[jetuse-dev-terraform-resources-ok]]）。
   出力を `runs/<run-id>/e2e/deploy.log` に保存する。
2. **複数シナリオ E2E**: `tasks/<id>.md` の「E2E シナリオ」を `e2e.min_scenarios` 本以上実行し、
   各シナリオの実行コマンド・期待結果・実結果（HTTP応答 / DB状態 / スクショ等）を
   `runs/<run-id>/e2e/scenario-<n>.*` に証跡として残す。
3. **ベストエフォート＋理由の明記**: タスク特性で E2E 不能・限定的な範囲は、
   `runs/<run-id>/e2e/SKIPPED.md` に「何を・なぜ実施できないか」を明記する（理由なしの省略はしない）。
4. **証跡込みレビュー**: その後に codex-review を起動する。`run_codex_review.sh` は diff に加えて
   `runs/<run-id>/e2e/` の証跡を Codex 入力に添付し、Codex は証跡の十分性も含めて採点する。
5. **人間ゲート**: jetuse-dev へのデプロイ（Terraform apply 含む）は承認済み。ただし IAM/テナンシ変更、
   既存リソース（VCN develop / インスタンス dev / バケット）変更、コミット/PR/push は引き続き人間ゲート。

## 完了ゲートで人間へ返すもの（**HTML は作らない**）

**報告書はオーケストレータが書く。ループは HTML を作らない**（2026-08-01 変更）。
理由: ループと オーケストレータの双方が報告書を作って二重になり、かつ実装の文脈に近すぎて
実装者視点（判定・差分・findings の羅列）になりやすかった。施主の評価は
「オーケストレータが書いたものの方が読みやすい」。**様式の正本は `references/report-style.md`**
だが、それを使うのはオーケストレータ側であり、ループは読まなくてよい。

完了ゲート（`review_verdict=PASS` かつ test/lint 緑 かつ実環境 E2E 通過）または人間ゲートで
停止したら、**最終メッセージに次を構造化して返す**。HTML 生成・レンダリング確認・報告パイプへの
配置は**行わない**（その分のトークンを使わない）。

| 返すもの | 中身 |
| --- | --- |
| `task` | タスク ID |
| `verdict` | `review_verdict` と最終 review 番号（例 `PASS (review-7)`） |
| `checks` | test / lint / build の結果（件数つき） |
| `e2e` | シナリオごとに「何を確かめて / 結果 / 証跡パス」。未実施は `SKIPPED.md` の理由も |
| `residual` | 非 blocker の指摘を severity・`file:line`・内容で列挙（**要約しない**） |
| `human_gates` | 残る人間ゲート（コミット / PR / push / 判断が要る点） |
| `decisions_needed` | 判断が要る点があれば、**選択肢と各々の影響**まで（オーケストレータが施主へ出す材料） |
| `surprises` | 想定と違ったこと・自分の誤りに気づいた箇所（あれば）。**隠さない** |

`STATE.md` には従来どおり全部書く（ローカル・git 追跡外。完了時に `runs/<run-id>/STATE.md` へ写す）。
オーケストレータはこの構造化された事実と `STATE.md` / `runs/` を材料に報告書を書く。

## STATE.md の扱い（git 追跡外）
`STATE.md` は**この worktree のローカル状態**であり、コミットしない（`.gitignore` 済み）。
タスクごとの worktree が同じパスを全面上書きするため、追跡すると並列ループを統合するたびに
必ず衝突する。停止（手順6）の直前に **`runs/<run-id>/STATE.md` へ写しを残す**こと。
写しは run 配下なのでタスク間で衝突せず、後から状態を辿れる。

## goal 完了条件との関係
完了条件は起動時の `GOAL` env（`runs/<run-id>/goal.txt` に記録）と `loop-config.yml` の
`goal_template` で与えられる。ループを止めてよいのは、STATE.md の `review_verdict == PASS` かつ
該当 area のテスト・lint がクリーン かつ実環境 E2E 通過のとき（手順6）。実装者が「できた」と
思っても、Codex が PASS を出すまで完了にはならない。

## なぜこの順番か
採点を実装者と分けることで「完了」が主張ではなく証明に近づく。履歴を残すのは、
後で loop-doctor がこの仕組み自体を改善できるようにするため（runs/ が唯一の根拠資料）。
