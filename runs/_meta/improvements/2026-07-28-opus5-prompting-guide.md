# 2026-07-28: Opus 5 プロンプティングガイド適合

- **入力（人間の指示）**: 公式ガイド
  https://platform.claude.com/docs/ja/build-with-claude/prompt-engineering/prompting-claude-opus-5
  に従い、ループエンジニアリングの maker 側プロンプトを現行モデル（Fable 5 = Opus 5 系）へ適合させる。
- **経緯**: loop-doctor の通常経路（runs/ の症状診断）ではなく、モデル世代交代に伴う予防的調整。
  プランをユーザーが承認済み（自己検証の両方無効化・重複整理まで実施・docs 乖離も修正）。

## 変更内容と根拠（ガイド該当節）

| 変更 | 対象ファイル | ガイド該当節 |
| --- | --- | --- |
| ponytail-review 自己レビュー（旧手順2.5）を削除、`self_review: false` | `.claude/skills/loop-protocol/SKILL.md`, `loop-config.yml` | タスクのスコープと過剰な検証 |
| `superpowers:verification-before-completion` を on_demand_skills から削除 | `loop-config.yml` | 同上（自己修正） |
| ターン出力・成果物長の簡潔指示を追加（「ターン出力の書き方」節） | `.claude/skills/loop-protocol/SKILL.md` | ユーザー向けの進捗更新／成果物ドキュメントの長さ |
| エージェント起動プロンプトに完全なタスク仕様を先渡し＋出力抑制の1文 | `.claude/skills/loop-runner/SKILL.md` | 機能の改善（エージェント的コーディング）／進捗更新 |
| サブエージェント制御（1体=1タスク・小さい作業は委譲しない・検証用に増やさない） | `.claude/skills/loop-runner/SKILL.md` | サブエージェントの生成の制御 |
| 同一指示の多重記述を一本化（verdict 不可侵 6→各スキル1、E2E 本数→`e2e.min_scenarios` 参照、ハードゲート一覧→loop-config 参照） | loop-protocol / codex-review / loop-runner / stage-runner の各 SKILL.md | 全体（指示の文字通り遵守・過剰強調の回避） |
| 手順番号の正規化（2.5/5.5 → 1..7）と手順2 過負荷の「実装規律」節への分離 | `.claude/skills/loop-protocol/SKILL.md` | 同上 |
| ステージ報告・レポートの長さ較正1行 | stage-runner SKILL.md / stage-report-template.md | 成果物ドキュメントの長さ |
| 存在しない `/goal` スラッシュコマンド前提の記述を実装どおりに修正 | `docs/loop-engineering.md`, loop-protocol「goal 完了条件との関係」 | （ガイド外・docs 乖離の是正） |

## 維持したもの（意図的に変えていない）
- codex-review の構造（別モデル Codex による maker/checker 分離）＝「自己検証の重複」に該当しない。
- `run_codex_review.sh` の INSTRUCTIONS / `review-schema.json`（Codex 向けプロンプト。Opus 5 ガイド適用外）。
- hooks・`.claude/loop/*.sh`・herdr 方式Bのランブック・task-packet-template.html・loop-doctor SKILL.md。

## 既知の未対応（今回の範囲外）
- `budget.max_turns` を読んで停止する主体が未実装。
- `test_cmd` 既定が web 固定（api タスクがフォールバックすると誤コマンドで「緑」になり得る）。

## 検証
プロンプト変更の実効性（レビュー品質・停止規律・トークン消費）は次の実 run の
`runs/<id>/`（reviews / turns / manifest.totals）で判定する。悪化が観測されたら loop-doctor へ。
