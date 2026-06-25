# 症状 → 構成要素 → 編集対象 対応表

loop-doctor が診断時に引く対応表。証跡（runs/ のファイルパス）で裏を取ってから推薦する。

## State / Memory
- 症状: 同じ指摘の再発、前回修正の忘却、重複作業。
- 証跡: 複数 run の reviews に同一 finding（同一 file:line / 同一 issue）。turns で過去参照なし。
- 編集対象: `STATE.md` の構造、`loop-protocol/SKILL.md` に「着手前に直近 reviews を grep」を追加。

## Sub-agent（Codex レビュアー）
- 症状: バグ見逃し（甘い）／過剰指摘で進まない（厳しすぎ）。
- 証跡: `review-*.json` の verdict と severity_counts の偏り、`.raw.txt` との突き合わせ。
- 編集対象: `codex-review/SKILL.md` の観点・重大度定義、`run_codex_review.sh` の INSTRUCTIONS、
  `review-schema.json`、`CODEX_MODEL`、`loop-config.yml` の diff_scope。

## Skills（知識）
- 症状: 規約・命名・ビルド手順を毎回間違える。
- 証跡: reviews に規約系 finding が頻出。
- 編集対象: `loop-protocol/SKILL.md` や規約スキルへ明文追記（「なぜそうするか」も書く）。

## Automations / /goal 完了条件
- 症状: 終わらない／空回り／早すぎる完了。
- 証跡: turns の `goal_checker.reason` の繰り返し、または PASS なのに受け入れ条件未達。
- 編集対象: `loop-config.yml` の `goal_template` の述語を厳密化、`max_turns`・しきい値。

## Worktrees
- 症状: 並行作業でのファイル衝突・上書き。
- 編集対象: worktree 分離の導入（git worktree か subagent の isolation: worktree）。Stage 3 で。

## Plugins / Connectors
- 症状: テスト/CI/課題管理に届かない、手作業が残る。
- 編集対象: MCP・コネクタ追加、`.claude/settings.json` の allowlist。

## コスト横断
- 症状: トークン浪費・冗長呼び出し。
- 証跡: `manifest.totals.tokens`、turn ごとの差分サイズ、レビュー回数。
- 編集対象: `loop-config.yml` の diff_scope の絞り込み、レビュー頻度、不要な subagent の削減。

## hooks / 履歴記録そのもの
- 症状: turns/reviews が記録されない、空の run が量産される。
- 証跡: `runs/<id>/` に欠損、または LOOP_TASK 未設定なのに run が生成。
- 編集対象: `.claude/hooks/session_start.sh`・`log_turn.sh` の活性化条件（LOOP_TASK ガード）。
