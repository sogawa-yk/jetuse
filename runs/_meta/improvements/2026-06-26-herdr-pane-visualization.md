# 改善: 並列モードの分業を herdr ペインで可視化

- 日付: 2026-06-26
- 起点: loop-doctor（人間指摘「各エージェントへの分業時に herdr のペイン分割で各エージェントが何をしてるか可視化したい」）
- 承認: 選択肢 A（推薦1=方式B のみ適用）

## 症状
並列モードの分業が Agent ツール（UI 面を持たない in-process サブエージェント）で起動されており、
herdr 内で回していても各エージェントの作業がペインに現れず、ライブで何をしているか見えない。

## 履歴上の証跡
- `.claude/skills/loop-runner/SKILL.md:22-40`（旧）= 並列は `isolation: worktree` サブエージェントを Agent ツールで起動。
- `grep -ril herdr .claude/skills .claude/loop` = 0 件（可視化導線が皆無）。
- 実行環境は `HERDR_ENV=1`（可視化基盤はあるのに未使用）。
- `.claude/loop/start-loop.sh:14-58` が既にタスク=1 worktree で `exec claude` する（ペイン起動にそのまま再利用可）。
- 対象 run（並列波の例）: `runs/2026-06-25T1544_PLG-04/`, `runs/2026-06-25T1545_PLG-06/`（turns 空＝低可視性）。

## 根本原因
並列モードが UI 面を持たない Agent ツール経路に固定されていたこと。herdr 連携が仕組みに未組込み。

## 変更内容
- `.claude/skills/loop-runner/SKILL.md` 並列モード節を `HERDR_ENV` で分岐:
  - 方式B（`HERDR_ENV=1`・推奨）: タスクごとに `herdr pane split` →
    `start-loop.sh <task>`（worktree 隔離込み）で実 claude を起動し goal プロンプトを流し込む。
    オーケストレータは `herdr wait agent-status --status done` で待ち合わせ、`herdr pane read` で
    最終構造化メッセージを回収。`blocked` は人間ゲート待ちとして拾う。
  - 方式A（非 herdr）: 従来どおり Agent ツール `isolation: worktree`（フォールバック）。
  - 「各エージェントへ渡すプロンプト」「タスク実行契約」「人間ゲート」は両方式共通のまま維持。
  - 後始末に `end-loop.sh <task>` + `herdr pane close` を明記。
- 同ファイル「原則」の worktree 隔離の記述を方式分岐に合わせて更新。

## 不採用（今回見送り）
- 推薦2（Agent ツール維持＋ログ tail 監視ペイン）= ライブ性が弱いため不採用。
- 推薦3（`loop-config.yml` に `visualize.herdr` トグル外出し）= 今回は SKILL.md 内の HERDR_ENV 分岐で十分なため見送り。必要なら次回。

## 検証
次の並列波（実行可能集合が2つ以上になる波）で loop-runner を回し、各タスクが個別ペインに
`working`/`done` で表示され、`herdr pane read` で作業内容が追えることを確認する。
