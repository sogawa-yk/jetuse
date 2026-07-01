---
name: stage-runner
description: STAGE<N>-PROGRESS.md のステージ全体を自走で実装し切る最上位ループ。loop-runner の上位で、人間ゲートを「タスク単位」から「ステージ単位」へ引き上げる。PASS したタスクをステージ専用ローカルブランチに自動 commit+merge して波を繋ぎ、キュー枯渇まで自走し、ステージ完了で1回だけ人間に報告・チェックを仰ぐ。push/PR/apply/ADR承認/IAM は自走中も停止。「ステージごとに承認で回したい」「ステージを丸ごと実装して最後に報告して」等を頼まれたら使う。
---
# stage-runner：ステージ承認ループ（最上位オーケストレータ）

あなたは**ステージ単位**で回す最上位オーケストレータ。各タスクの実行は `loop-runner` に委譲し、
各タスクの中身は `loop-protocol` / `codex-review` に従う。loop-runner との違いは1点:
**人間ゲートをタスク単位ではなくステージ境界に集約する**。ステージ内の波はあなたが自動で繋ぐ。

- 進捗の単一の真実源: `tasks/STAGE<N>-PROGRESS.md`（順序・依存・ゲート・status）。
- 自動統合の隔離先: **ステージ専用ローカルブランチ `feat/stage-<N>`**（`feat/loop-engineering` から分岐）。
  ここにだけ commit+merge する。**リモート push / base への PR / apply は一切しない**ので、
  人間チェック前に base もリモートも汚れない。
- 設定は `loop-config.yml` の `stage_runner:` ブロック。

## 起動前提
推奨は `.claude/loop/start-stage.sh <stage-id>` で起動すること。これは統合ブランチ＋ worktree を用意し、
**commit/merge は許可・push/PR/apply/destroy は権限層で遮断**したオーケストレータ claude を統合 worktree 内に起動する。
（`begin_stage.sh` が `feat/stage-<N>` と worktree `_<stage>` と `runs/_stages/<stage>/` を作る。）

## 手順
1. **準備**: `STAGE<N>-PROGRESS.md` を読む。統合ブランチ worktree が無ければ
   `.claude/skills/stage-runner/scripts/begin_stage.sh <stage>` で用意する（start-stage.sh 経由なら済）。
2. **波ループ（キュー枯渇まで繰り返す）**:
   1. **実行可能集合**を求める = 依存がすべて done かつ status=todo。空なら波ループ終了（手順3へ）。
   2. その集合から最大 `stage_runner` 同時数（既定は loop-runner と同じ最大3）を選び、**loop-runner の方式B/A で並列起動**。
      **`HERDR_ENV=1` のときは方式B必須**：実行可能集合が**1タスクだけの直列波でも**専用ペインで起動し、
      **オーケストレータのペイン内でタスク本体を inline 実行しない**（main 左半分・タスク右列の可視化を常時維持）。
      ただし **base ブランチは `feat/stage-<N>`**（`BASE_BRANCH=feat/stage-<N>` を start-loop.sh に渡す）。
      これで前波の自動統合結果を後続タスクが見られる（依存解決）。各タスクは `LOOP_AUTONOMOUS=1`。
   3. 各タスクの完了を待ち合わせる。タスクが **review_verdict=PASS かつ test/lint クリーン かつ 実環境 E2E 通過**で
      停止したら、そのタスクを**自動統合**する（手順4）。**ハードゲート**で止まったタスクは status=`blocked` にし、
      理由（どのゲートか）を記録して**他タスクを進める**（手順5）。
   4. 統合後、`STAGE<N>-PROGRESS.md` の当該タスクを **status=done** に更新する（人間承認を待たない＝ここが loop-runner との差）。
   5. 1 に戻る。
3. **ステージ報告で停止**（ヒューマンゲート）。`references/stage-report-template.md` に従い
   `runs/_stages/<stage>/REPORT.md` を書き、人間に提示して**チェックを仰ぐ**。**自分で base へ PR/push しない**。

## 自動統合（PASS タスク → ステージブランチ）
`.claude/skills/stage-runner/scripts/integrate_task.sh <stage> <task>` を使う。これは:
- タスク worktree で deliverable をコミット（**STATE.md / runs / packages/web/dist / .current_run_id は除外**）。
- `feat/<task>` を `feat/stage-<N>` へ**ローカル merge**（push しない）。
- 自動コミットは**オーケストレータ（このスキル）だけ**が行う。タスクエージェントの権限 deny（commit/push/merge）は据え置き＝多層防御。

統合したら**統合 worktree で area の test/lint を再実行**して緑を確認する（緑でなければ修正タスクとして扱い、
直せなければ手順3のハードゲート同様にステージ報告で提示）。

### コンフリクト時（`conflict_policy: subagent_resolve_then_review`）
`integrate_task.sh` が **exit 3** を返したら（統合 worktree は merge 進行中のまま）:
1. **サブエージェントを起動して解決を試行**する（Agent ツール / 統合 worktree 内）。解決方針は実装の意図を保つこと
   （例: SBA-03/04 のような意味的衝突は、両者の契約を壊さない統合に倒す）。
2. 解決後、**`codex-review` を回す**（衝突解決そのものをレビュー対象にする）。
3. **Codex PASS かつ area test/lint クリーンなら** merge をコミットして継続。
4. **不能/FAIL なら** `git merge --abort` して当該タスクを status=`blocked`（理由=統合衝突）にし、ステージ報告で提示。

## ハードゲート（自走中も必ず停止・越えない）
`loop-config.yml` `stage_runner.hard_gates`:
- **push**（リモート push） / **pr_to_base**（base への PR・merge） / **terraform_apply**（apply・課金） /
  **billing** / **iam_identity**（IAM・Identity Domain） / **adr_approval**（真の決定を伴う ADR 承認）。

当該ゲートに当たったタスクは**越えずに** status=`blocked`、理由を記録して他タスクを進める。
- **ADR**: ドラフト作成は進めてよい（成果物）。**承認**は越えない＝ステージ報告でまとめて提示。
  ただしテナンシ/IAM/ポリシー系 ADR はドラフト時点でも慎重に扱い、判断を仰ぐ。
- **デモ品質ゲート**（SBA 系・HBD-05 等）: 自動では合格判定しない。実装・E2E まで進めて
  **ステージ報告で一括レビュー**を仰ぐ（＝ご要望の「ステージ完了時に1回チェック」）。

## ステージ報告（出口・ヒューマンゲート）
`runs/_stages/<stage>/REPORT.md` に集約して提示する（テンプレ: `references/stage-report-template.md`）:
- 全タスクの {status, review_verdict, E2E 結果, 証跡パス `runs/<run-id>/e2e/`}。
- 統合ブランチ `feat/stage-<N>` の差分サマリ（base 比）。
- **残ハードゲート一覧**（push/PR・apply・ADR 承認・blocked タスク）＝人間が次に何を承認すれば base へ出せるか。
- 人間は `feat/stage-<N>` を見てチェック → 承認後に **base への PR / push** を人間（または別途指示）で実施。

## 原則
- **隔離**: 自動統合は `feat/stage-<N>` 限定。リモート/base/apply には絶対に出さない。
- **ゲートは飛ばさない**: タスク単位ゲートをステージ境界に集約するだけ。push/PR/apply/ADR/IAM は据え置きで停止。
- **証跡主義**: タスクごとに Codex PASS＋実環境 E2E 必須。統合後も test/lint 緑を再確認。
- **むやみに増やさない**: ステージ worktree は1本、loop ADB は再利用（タスクは `JETUSE_<task>` スキーマ隔離）。
- 後始末: ステージ承認・base マージ後に `end-loop.sh <task>` で各タスク worktree を、
  `git worktree remove` で統合 worktree を撤去する。

## 使い方の例
`.claude/loop/start-stage.sh stage-2` で起動 → セッション内で本スキル（/stage-runner）を実行 or
「ステージ2を回して、終わったら報告して」と指示 → HBD-01 →（HBD-02・HBD-03 並行）→ HBD-04 → HBD-05 を
自走で統合 → `runs/_stages/stage-2/REPORT.md` で停止・報告。
