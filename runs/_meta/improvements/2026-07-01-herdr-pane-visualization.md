# 改善記録: herdr ペイン可視化が直列波で無効化する

- **日付**: 2026-07-01 / loop-doctor
- **入力（施主の症状）**: herdr のペイン分割で各エージェントの作業が見えない（無効に見える）。
  main を左半分に固定・その他は右側に分割表示したい。
- **承認**: オプション A（①＋②のみ）。③（ヘルパスクリプト）は不採用。

## 履歴上の証跡
- `HERDR_ENV=1`。`herdr pane split … --direction right` を実測 → `result.pane.pane_id` 取得可。分割機構自体は正常。
- `tasks/STAGE0-PROGRESS.md:16`「依存が直列なので1波1タスクで進む（EXB-00 → EXB-01 → EXB-02）」。
- `runs/2026-06-30T_stage0-EXB-0{0,1,2}/` がメイン worktree `/home/opc/jetuse/runs/` 直下に生成
  （`git worktree list` に EXB-0x 無し）→ 専用ペイン/start-loop.sh を経ず inline 実行された証拠。
- 対照: `../jetuse-loops/FE-01`（stage-5 並列タスク）は worktree 存在 → 並列波では方式Bが機能していた。

## 根本原因
ペイン可視化（方式B）が「複数タスクの並列起動経路」にしか結線されておらず、1波1タスクの直列ステージでは
オーケストレータがタスクを自ペイン内で inline 実行 → 専用ペインが生成されず可視化が出ない。
望みのレイアウト（main 左半分固定／他は右列縦積み）は `loop-runner/SKILL.md:34-41` に既に定義済みで、
壊れていたのは「常に方式Bを通す」という発火条件のみ。

## 変更（適用済み）
- **① `.claude/skills/loop-runner/SKILL.md`**
  - 方式B冒頭に「`HERDR_ENV=1` なら波が1タスクでも必ず専用ペイン。inline 実行禁止」を追記。
  - 方式Aを「`HERDR_ENV` 非設定のときのみ」に限定（`HERDR_ENV=1` では方式Aを使わない旨を明記）。
- **② `.claude/skills/stage-runner/SKILL.md`（手順2-2）**
  - 「`HERDR_ENV=1` は方式B必須。1タスクの直列波でも専用ペイン、inline 実行しない」を追記。

## 検証（次 run で）
次にステージ（特に直列ステージ）を回す際、各タスクが専用ペインで起動し、main が左半分・タスクが右列に
縦積みされることを確認する。inline 実行で `runs/` がメイン worktree に落ちていないこと（作業ツリー分離）も確認。
