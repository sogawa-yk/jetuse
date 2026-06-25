---
name: loop-runner
description: tasks/STAGE1-PROGRESS.md のキューを依存順に1つずつ消化する上位ループ。次に実行可能なタスクを選び loop-protocol で実装→codex-review→記録し、人間ゲートで停止する。「タスクを順番に実施して」「キューを回して」等を頼まれたら使う。完全無人ではなくゲートで停止するセミオート。
---
# loop-runner：タスクキューの逐次実行（上位ループ）

あなたは複数タスクを順に回す**オーケストレータ**。各タスクの中身は `loop-protocol` / `codex-review` に従う。
進捗の単一の真実源は `tasks/STAGE1-PROGRESS.md`（順序・依存・ゲート・status）。

## 手順
1. `tasks/STAGE1-PROGRESS.md` を読む。**依存がすべて done で status=todo の先頭タスク**を1つ選ぶ
   （並行可でも、このセッションでは1つずつ）。実行可能なものが無ければ終了（全 done か、残りは blocked）。
2. `GOAL="<完了条件>" .claude/skills/loop-runner/scripts/begin_task.sh <task-id>` を実行する。
   このスクリプトが **タスク用ブランチ `feat/<task>` を base(`feat/loop-engineering`)から自動で切り**（`ensure_task_branch.sh`）、
   そのタスク用の run-id を採番する（同一セッションでも履歴・ブランチがタスク単位で分かれる）。
   - 追跡ファイルに未コミット変更が残っているとブランチ切替は中断する（前タスクの取りこぼし防止）。
     その場合はコミット/PRゲート（手順6）が未処理ということ → 先にそれを片付ける。
   - 依存タスクは依存先が base にマージ済みであること。連鎖したい場合は `BASE_BRANCH=feat/<dep>` を前置。
4. `tasks/<task>.md` の受け入れ条件と `loop-config.yml` の `goal_template` から完了条件を組み、
   STATE.md の「現在のタスク」に記入する。
5. `loop-protocol` を回す: 実装 → `codex-review` → `runs/<id>/` と STATE.md に記録 → FAIL は次ターンで修正。
   **review_verdict=PASS かつ 当該 area の test/lint クリーン**になるまで。review_verdict を自分で書き換えない。
6. **人間ゲートでは必ず停止する（自動で越えない）**:
   - コミット / PR / push（全タスク共通）
   - ADR 承認（PLG-01）／ Terraform apply・課金（PLG-04）／ デモ品質（SBA-02, PLG-08）／ VLM 前提（SBA-05）
   停止時は「**何を承認すれば次に進めるか**」を明示して待つ。
7. 人間がゲートを通したら（コミット承認・ADR承認等）、`tasks/STAGE1-PROGRESS.md` の当該タスクを
   `done` に更新し、**1 に戻る**。
8. 依存未達・能力前提なし等で進めない場合は status=`blocked` にして理由を書き、次の実行可能タスクへ。

## 原則
- 一度に1タスク。並行は人間が別セッションで行う。
- Stage は `loop-config.yml` に従う（既定 report-only ＝ コミットしない）。**無人度を上げる（auto-fix/auto-commit）のは人間ゲート**。
- 完全無人化はしない。ゲートを飛ばさないことがこの仕組みの価値。

## 使い方の例
`LOOP_TASK=stage1 claude` で起動し、セッション内で「tasks のキューを順番に実施して」と指示するか、
本スキルを起動する。以降、上の手順をタスクが尽きるまで繰り返す。
