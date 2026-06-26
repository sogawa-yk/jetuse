# ループ改善: ステージ承認ループ（stage-runner）の新設

**日付:** 2026-06-26
**起票:** loop-doctor（人間リクエスト）
**対象 run/状況:** ステージ2（HBD-01..05）起票直後。ステージ1完了の整合後。
**承認:** 人間承認済み（推奨どおり全採用 / コンフリクト方針=その都度サブエージェント解決試行）。

## 症状（観測された要望・ギャップ）
- `loop-runner` は実行可能集合を並列実行するが、**タスクごとに人間ゲート**（コミット/PR/push・ADR・apply/課金・デモ品質）で停止する。
- 望み: 上位に「**ステージ単位の承認ループ**」。ステージ内の全タスクを自走で実装し切り、**ステージ完了で1回だけ人間に報告・チェック**。

## 履歴上の証跡（根拠）
- `.claude/skills/loop-runner/SKILL.md` §手順4 = 「人間ゲートで必ず停止」「波が終わってゲートを人間が通したら次の波へ」→ 人間が波ごとに介在。
- `.claude/loop/start-loop.sh` L60-70 = `LOOP_AUTONOMOUS=1` のタスクエージェントは権限層で commit/push/merge/pr/apply を deny（保持すべき安全策）。
- `.claude/hooks/ensure_task_branch.sh` ＋ `start-loop.sh` = タスク=worktree `feat/<task>`、依存タスクは依存先が base にマージ済みである必要 → 波間に中間マージが必須。
- `loop-config.yml` `stage: report-only`（自動コミットしない。引き上げは人間ゲート）。
→ ステージ承認ループには「PASS タスクをステージ専用ブランチへ自動 commit+merge して波を繋ぐ」自動統合が必要。これは report-only→auto-commit の段階引き上げに当たり、本リクエストがその承認。範囲は厳しく絞る。

## 根本原因
人間ゲートが**タスク粒度**なのは (1) 依存解決に中間マージが要る (2) コミットが人間ゲート、の2点。
→ ハードゲート（push/PR/apply/ADR/IAM）は越えないまま、**stage 専用ローカルブランチへの自動統合**を導入して解決。

## 適用した変更（承認済み・全採用）
| 対象ファイル | 変更 |
|---|---|
| **新規** `.claude/skills/stage-runner/SKILL.md` | 最上位オーケストレータ。波ループ→PASS で自動統合→status=done→キュー枯渇まで自走→ステージ報告で停止。ハードゲートは blocked にして他を進める。コンフリクトはサブエージェント解決→codex-review。 |
| **新規** `.claude/skills/stage-runner/scripts/begin_stage.sh` | `feat/stage-<N>` を base から作成/再利用し、統合 worktree `_<stage>` と `runs/_stages/<stage>/` を用意。 |
| **新規** `.claude/skills/stage-runner/scripts/integrate_task.sh` | PASS タスクの deliverable をコミット（STATE.md/runs/dist/.current_run_id 除外）→ `feat/<task>` を `feat/stage-<N>` へローカル merge。コンフリクトは自動解決せず exit 3。push/PR/apply はしない。 |
| **新規** `.claude/skills/stage-runner/references/stage-report-template.md` | ステージ報告テンプレ（タスク別結果・統合差分・残ハードゲート・コンフリクト記録・次アクション）。 |
| **新規** `.claude/loop/start-stage.sh` | オーケストレータ起動。commit/merge 許可・**push/gh pr/terraform apply・destroy は deny**。統合 worktree 内で claude 起動。 |
| **編集** `loop-config.yml` | `stage_runner:` ブロック追加（integration_branch / autonomy=auto_commit_to_stage_branch_only / hard_gates / conflict_policy=subagent_resolve_then_review / report_at=stage_completion / stage_token_budget）。グローバル `stage: report-only` は据え置き。 |
| **編集** `CLAUDE.md` | ループ節に「ステージ承認ループ（stage-runner）」を追記。 |
| **編集** `tasks/STAGE2-PROGRESS.md` | 実行方式の選択（loop-runner / stage-runner）と status 更新タイミングの注記。 |

## 設計の要点（安全策）
- **隔離**: 自動統合は `feat/stage-<N>` ローカル限定。リモート/base/apply には絶対出さない。
- **多層防御**: 自動コミットはオーケストレータのみ。タスクエージェントの権限 deny は据え置き。
- **ハードゲート据え置き**: push / pr_to_base / terraform_apply / billing / iam_identity / adr_approval は自走中も停止。
- **証跡主義**: タスクごとに Codex PASS＋実環境 E2E 必須。統合後も area test/lint 緑を再確認。
- **コンフリクト**: 自動解決せず、サブエージェント解決→codex-review→緑なら継続/不能なら git merge --abort して blocked。

## 次の検証
- ステージ2（HBD-01..05）を `.claude/loop/start-stage.sh stage-2` で回し、`runs/_stages/stage-2/REPORT.md` に
  全タスクの verdict/E2E/残ゲートが集約され、`feat/stage-2` がローカル統合のみ（未 push）であることを次 run で確認する。
