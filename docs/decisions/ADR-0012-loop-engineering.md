# ADR-0012: ループエンジニアリングの導入（Claude Code × Codex）

日付: 2026-06-25
状態: 提案（人間レビュー待ち）

## 背景

`loop-impl.md`（v1.0）が「ループエンジニアリング」の実装設計を定義した。
Claude Code を実装者（maker）、Codex をレビュアー（checker）とし、`/goal` の完了判定モデルで
停止条件を採点する三層構成。本リポジトリへ scaffold を導入するにあたり、設計書の
スケルトン（付録 A）を実環境で検証した結果、いくつか具体化・改良を要する差分が判明した。

設計書のコマンド文字列は「実バージョンで検証すること」と明記されているため、本 ADR に実測と
採用判断を残す。

## 実環境の確定事実（2026-06-25 検証）

- Claude Code `2.1.191`（`/goal` は v2.1.139 で追加済 → 利用可）。
- codex-cli `0.142.1`。`codex exec` に以下を確認:
  - `--output-schema <FILE>`（最終応答を JSON Schema に強制）
  - `--output-last-message <FILE>` / `--json`（JSONL イベント）
  - `--sandbox <MODE>`（`read-only` でファイル変更を禁止）
  - `codex exec review`（`--uncommitted` / `--base` / `--commit`）
  - stdin パイプは `<stdin>` ブロックとして PROMPT に追記される。
- モノレポ: `packages/web`（test=`vitest run`, lint=`eslint .`）/ `packages/api`（pytest, ruff）。

## 決定（設計書からの差分）

1. **CLAUDE.md は置換せず追記。** 設計書 A-1 は「ループ憲法」で CLAUDE.md 全体を置く前提だが、
   本リポジトリの CLAUDE.md は既存の運用ルールの正本。ループ運用は専用セクション追記＋
   `docs/loop-engineering.md` への分離とした。

2. **Codex レビューは `--output-schema` で構造化 JSON を直接生成。** 設計書 A-7/A-8 は
   「生出力 → 生成AIで再抽出」だが、抽出の取りこぼしを避けるため `review-schema.json` で
   スキーマを強制し、スクリプトがメタデータで包んで `review-<n>.json` を確定する。
   生出力（`.raw.txt`）と入力差分（`.input.diff`）も監査用に対で保存（履歴設計 §5「入力と出力の両方」）。
   blocker>0 のとき verdict を機械的に FAIL へ矯正する。

3. **差分は stdin で渡す。** CLI 引数だと ARG_MAX 超過リスクがあるため。

4. **codex は `--sandbox read-only` で実行。** レビューが書き込み・送信を伴わない（人間ゲートの前提）。

5. **loop モードの活性化スイッチを `LOOP_TASK` 環境変数に。** 設計書の hooks は全セッションで
   発火し runs/ を汚す懸念がある。`LOOP_TASK` 未設定時は session_start / log_turn を完全 no-op とし、
   通常の開発セッションに一切影響させない。残骸 `.current_run_id` も通常セッション開始時に掃除。

6. **モノレポ対応で test/lint コマンドを area 別に定義。** `loop-config.yml` の `areas.{web,api}` に
   test_cmd / lint_cmd / build_cmd を持たせ、`goal_template` の `{test_cmd}` に当てる。

7. **Stage 1（report-only）から開始。** Claude は実装するがコミットしない。Codex レビューと
   履歴記録のみを自動化。段階引き上げは人間ゲート（§7）。

8. **`.current_run_id` は gitignore。** セッション固有の一時ファイル。runs/ の履歴は追跡対象。

## 影響

- 新規ファイル: `loop-config.yml` / `STATE.md` / `tasks/_template.md` / `.claude/`（settings・skills・agents・hooks）/
  `runs/`（雛形）/ `docs/loop-engineering.md`。CLAUDE.md にセクション追記。
- `.claude/settings.json` の hooks は本リポジトリ全セッションに適用されるが、`LOOP_TASK` ガードにより
  loop モード以外では no-op。
- 未検証事項: `codex exec` の実モデル呼び出し（認証・コスト・出力品質）は人間承認下での実走で確認する
  （`docs/verification/` に LOOP-01 として残す）。`/goal` のオーバーレイ挙動も実走で確認。

## 参照

- `loop-impl.md`（v1.0 / 2026-06-25、リポジトリ未追跡 = .gitignore）
- Addy Osmani "Loop Engineering"（2026-06-07）
- `codex exec --help` / `codex exec review --help`（0.142.1 実測）
