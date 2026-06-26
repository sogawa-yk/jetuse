# 改善: 無人ペイン起動の自律化＋/goal 実態整合（loop-doctor 2026-06-26）

## 症状（人間の指摘）
1. ペインで起動したエージェントが、ユーザー承認フェーズ（ツール権限プロンプト）に阻まれ自律実行できない。
2. 起動エージェントが `/goal` で実行していないように見える。

## 履歴上の証跡
- `herdr pane read w1:pD` → `cd with output redirection - manual approval required` の "Do you want to proceed?" で停止。
  `w1:pE` → `.oci/` 読取の承認プロンプトで停止。実作業の最初のツールで即停止していた。
- `.claude/settings.json` の `permissions.allow` は 7 個のみ（codex-review / git diff / test / lint）。
- `.claude/loop/start-loop.sh:52`（旧）= `exec claude`（権限モード指定なし＝対話モード）。無人ペインで詰む。
- `/goal` スラッシュコマンドは実在せず（`find .claude` / `find ~/.claude` 共に空）。
  Stop hook `log_turn.sh` はターン記録のみ（goal 採点・再プロンプトをしない）。ループはエージェント自走。
- `/home/opc/jetuse-loops/{SBA-02,PLG-08}/runs/*/goal.txt` = プレースホルダ。起動時に `GOAL=` env 未付与だった。

## 根本原因
- RC1: `start-loop.sh` が常に対話的権限モードで起動 → 無人ペインで権限プロンプト停止。
- RC2: 方式B 起動で `GOAL=` env 付け忘れ → goal 未記録（実 goal はチャットプロンプトのみ）。
- RC2′: `/goal` は未実装。ドキュメントの「三層採点」表現が実態（GOAL env＋プロンプト＋loop-protocol 自走）と乖離。

## 適用した変更（承認: ユーザー「推奨一式＋ペインは(ii)再起動」 2026-06-26）
- **P1** `.claude/loop/start-loop.sh`: env ゲート `LOOP_AUTONOMOUS=1` を追加。立つと
  `exec claude --permission-mode bypassPermissions --disallowedTools "Bash(git commit:*)" "Bash(git push:*)"
  "Bash(git merge:*)" "Bash(gh pr create:*)" "Bash(gh pr merge:*)" "Bash(terraform apply:*)" "Bash(terraform destroy:*)"`。
  未設定（人間が付く逐次/worktree 起動）は従来どおり対話モード（通常開発・通常コミットを壊さない）。
  ハードゲート（コミット/PR/push/merge/apply/destroy）は権限層からも遮断＝「ゲートを飛ばさない」を保全。
- **P1/P2** `.claude/skills/loop-runner/SKILL.md` 方式B: 起動行を
  `LOOP_AUTONOMOUS=1 GOAL='<完了条件>' .claude/loop/start-loop.sh <task>` に統一。
  GOAL env 必須・LOOP_AUTONOMOUS 必須を明記。大プロンプトが `[Pasted text]` で未送信になる場合の
  `send-keys Enter` 回避手順も追記。
- **P3(a)** `SKILL.md` / `CLAUDE.md`: 「`/goal` スラッシュコマンドは未実装。ループはエージェントが
  loop-protocol を毎ターン辿って自走。完了条件は GOAL env（goal.txt 記録）＋プロンプトで与える」に是正。
  CLAUDE.md の「三層採点」表現を実態へ修正。

## 残存リスク / 次 run での検証ポイント
- `bypassPermissions` 下で `--disallowedTools` が確実に効くか（commit/push/apply が実際にブロックされるか）は
  次 run で実地確認する。万一バイパスされても、プロンプトの「コミットしない」指示が従来からの行動ゲート。
- 対象 run: 2026-06-26 Wave3 再起動分（SBA-02 / PLG-08、autonomous）。
