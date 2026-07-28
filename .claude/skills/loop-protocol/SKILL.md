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
   修正せず STATE.md とタスクパケットの「判断が要る事項」バナーに residual
   （後続/人間トリアージ）として file:line 付きで列挙する。磨き込みの反復は各修正が新たな
   助言を生んで止まらなくなる（実例: EXB-03 が PASS 後に12ラウンド・約220kトークン）。
   PASS 後にコードを変えるのは (a) 受け入れ条件の未達を埋める場合、
   (b) 人間/オーケストレータの明示指示がある場合のみ。
7. **人間ゲートで停止する。** コミット / PR / push は人間の承認後（CLAUDE.md）。
   停止時は terse なテキストではなく HTML タスクパケット（下記）を提示する。

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

## 人間ゲートに出すタスクパケット（HTML・完了ゲートで1回）
**書き方の正本は `references/report-style.md`（様式の選び方・図の描き方・提示前のレンダリング確認）。
報告書を書く前に必ず読む。** 要点:
- 実装タスクの完了報告は `references/task-packet-template.html`、**方式・設計の判断を仰ぐ報告
  （ADR 承認・やり方の変更・選択肢の提示）は `references/decision-packet-template.html`**（前提→なぜ→
  どのように→承認）。判断を仰ぐ報告に完了報告の様式を使わない（読み手は文脈を共有していない）。
- 図は HTML+CSS で描く（SVG で座標を手置きしない＝テキストが伸びると重なる）。詳細は `<details>` に畳む。
- **提示前に `scripts/check_report_render.sh <html>` でレンダリングし、出力 PNG を Read で見て
  重なり・見切れが無いことを確認する**（省略不可。Chrome 不在でスキップした場合は報告に明記）。

完了ゲート（`review_verdict=PASS` かつ test/lint 緑 かつ実環境 E2E 通過）に達したら、人間ゲートに出す
タスクパケットを `references/task-packet-template.html` からコピーして埋め、`loop-config.yml` の
`report.build_dir`（＝`runs/<run-id>/report/<TASK>.html`）に書き出す。これが per-task の唯一の
レビュー成果物（従来の 5〜20KB の密な散文レポートを置き換える）。

書き出したら**報告パイプで人間の閲覧場所へ配置する**（`loop-config.yml` の `report`・契約は
`docs/guides/report-pipe.md`）。報告書は仕様でも証跡でもないのでリポジトリに閉じない:
- `report.pipe: personal_skill`（既定）: ホーム側の個人スキル（既定 `preview`・env `LOOP_REPORT_SKILL`
  で上書き）を**配置モード**（`report.mode: place_only`）で呼ぶ。**完成済み HTML のパスを渡し、
  再生成・改変をさせない**（様式はリポジトリ側が持つ）。返る絶対パスと `![[<topic>.html]]` を
  ターンの最終メッセージに載せる。topic は `report.topic.task`。
- 個人スキル未導入 / `.obsidian-dir` 未設定で配置できないときは `report.fallback`（既定 `artifact`）:
  `build_dir` の HTML をそのまま Artifact 化して提示し（`artifact-design` skill を読んでから）、
  「報告パイプ未設定（docs/guides/report-pipe.md）」を1行添える。**ループは止めない。**

HTML はリポジトリにコミットしない（`.gitignore` で `runs/**/*.html` を塞いである）。

設計思想＝「例外だけ露出」。施主に読ませる面積を最小化し、判断が要る箇所だけ立てる:
- **何を・なぜ**: 製品/デモ目線で 3〜5 行。専門語（`§`参照・HTTP コード・内部識別子）は展開して、
  施主が読んで分かる言葉にする。「このデモにとって何が変わり、なぜ必要か」を書く（実装の羅列にしない）。
- **判断が要る事項バナー**（テンプレの `<div class="banner">`）: override / 未対応 residual / 後続未起票が
  あるときだけ残す。無ければブロックごと削除し、ヘッダ判定バッジを緑 PASS にする。例外があればバッジは琥珀「要判断」。
  override 時は迂回する具体 findings（id/severity/file:line）と理由をここに inline（codex-review 参照）。
- **Codex 判定**: clean PASS は判定1行のみ（`PASS (review-N) / blocker0 major0`）。全 findings は
  `<details class="aud">` に畳む＝監査用・人間の必読ではない（チェッカーを信頼する。信頼できないならチェッカーを直す）。
- **差分**: このタスク1件分の `git diff --stat` と「必読ファイル」1〜数点。
- **E2E**: 実施シナリオ数・結果・証跡パス。

> 原則: 人間の仕事は ①何を・なぜを読む ②例外だけ判断する ③挙動を E2E で確認する、の3点。
> clean PASS の findings 全読は求めない。プレースホルダ `{{...}}` はすべて埋めてから提示する。

## goal 完了条件との関係
完了条件は起動時の `GOAL` env（`runs/<run-id>/goal.txt` に記録）と `loop-config.yml` の
`goal_template` で与えられる。ループを止めてよいのは、STATE.md の `review_verdict == PASS` かつ
該当 area のテスト・lint がクリーン かつ実環境 E2E 通過のとき（手順6）。実装者が「できた」と
思っても、Codex が PASS を出すまで完了にはならない。

## なぜこの順番か
採点を実装者と分けることで「完了」が主張ではなく証明に近づく。履歴を残すのは、
後で loop-doctor がこの仕組み自体を改善できるようにするため（runs/ が唯一の根拠資料）。
