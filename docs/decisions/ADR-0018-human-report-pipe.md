# ADR-0018: 人間向け報告書 HTML の置き場をリポジトリ外（各自の閲覧環境）へ逃がす

日付: 2026-07-28
状態: 承認済み（2026-07-28 施主承認。承認判断そのものを新経路の HTML 報告で提示して確認）

## 背景

ループの完了ゲートで生成する人間向け HTML（`loop-protocol` のタスクパケット、`stage-runner` の
ステージ報告）は、これまでリポジトリ内の `docs/verification/<TASK>.html` に書き出していた。

問題が2つある。

1. **確認導線が割れる。** 施主は日常の資料を Obsidian vault で読む（共通規約の `preview` スキルは
   `_renders/<topic>.html` へ出す）。ループの報告だけがリポジトリ内にあり、統一して確認できない。
2. **ルールと実装が食い違っていた。** `CLAUDE.md` は「人が読む成果物は preview スキルで `_renders/` へ・
   HTML はコミットしない」と定めているのに、`stage-runner` の `integrate_task.sh` は
   `docs/verification/*.html` を除外していないため、ステージ自動統合でコミットされる状態だった。

さらに、現在の開発者は1人だが**将来増える可能性がある**。「Obsidian の特定 vault」は個人の環境依存で、
リポジトリに書き込むべき情報ではない。

## 決定

**報告書 HTML の「置き場の解決と配置」を各開発者のホーム側（`~/.claude/skills/`）スキルに委ね、
リポジトリは様式・中身・証跡だけを持つ。** 接点は細いインターフェース1本にする。

- 責務分離:
  - リポジトリ = 報告の**様式**（`task-packet-template.html` 等）、**中身の規定**（loop-protocol）、
    **判定の証跡**（`runs/<run-id>/`）。
  - 各自のホーム = **置き場の解決と配置**のみ（既定は `preview` スキルの配置モード）。
- 契約（`docs/guides/report-pipe.md` が正本）: 入力＝完成済み単一ファイル HTML の絶対パス＋topic、
  動作＝`<対象フォルダ>/_renders/<topic>.html` へ**そのまま配置**（再生成・改変禁止）、
  出力＝絶対パスと `![[<topic>.html]]`。
- 設定の単一真実源は `loop-config.yml` の `report:` ブロック（`build_dir` / `pipe` / `skill` / `mode` /
  `topic` / `fallback`）。個人差は `LOOP_REPORT_SKILL` env と `.obsidian-dir` で吸収し、
  `loop-config.yml` を書き換えさせない。
- 原本の生成先を `docs/verification/<TASK>.html` → **`runs/<run-id>/report/<TASK>.html`** に移す。
- 未設定の人（個人スキル無し / `.obsidian-dir` 無し）は fallback＝`build_dir` の HTML を Artifact 化して
  提示する。**報告パイプは完了ゲートではなく、ループを止めない。**

## 根拠

- 報告書は仕様でも証跡でもない。読む人の環境に置くのが正しく、リポジトリに閉じる必然性がない。
- 様式をリポジトリに残すことで、置き場が人ごとに違っても**報告の粒度と例外の出し方は全員で揃う**
  （「例外だけ露出」設計＝`loop-protocol` の資産を失わない）。
- 生成先を `runs/` 配下にすると、`integrate_task.sh` が既に `runs` を reset しているため
  **コミット混入の穴が追加改修なしで塞がる**（背景2の是正）。`.gitignore` で二重に塞ぐ。
- 個人スキル名や vault パスをリポジトリに書かないので、人が増えても各自が配置スキル1本を
  用意するだけで済む（リポジトリ側の変更が不要）。

## 影響

- 変更: `loop-config.yml`（`report:` 追加）、`loop-protocol` / `loop-runner` / `stage-runner` の
  報告節、`task-packet-template.html` ヘッダ、`stage-report-template.md`、
  `start-loop.sh` / `begin_stage.sh`（`.obsidian-dir` を worktree へ複製）、`.gitignore`、
  `CLAUDE.md`、`docs/loop-engineering.md`、新規 `docs/guides/report-pipe.md`。
- ホーム側（`~/.claude/skills/preview`）に配置モードを追加する（リポジトリ管理外）。
- `.obsidian-dir` は gitignore 済みで `git worktree add` では伝播しないため、起動スクリプトが複製する。
  これをしないと無人ループが出力先の確認プロンプトで停止する。

## 代替案と却下理由

- **リポジトリ内 `docs/verification/` のまま＋コミット除外だけ直す**: 実装は最小だが、確認導線が
  Obsidian と割れる問題（背景1）が残る。施主の一次要求を満たさない。
- **`report-html` 専用スキルをホーム側に新設**: 責務は明確だが、出力先解決ロジックが `preview` と
  2箇所に重複する。既存 `preview` に配置モードを1つ足す方が保守点が少ない。
- **生成もホーム側スキルに任せる**: 様式が個人ごとに散り、報告の粒度が揃わなくなる。
  「例外だけ露出」の設計が壊れるため却下。
- **リポジトリ生成＋Obsidian へコピーの二重管理**: 原本が2つになり、どちらが正かが曖昧になる。
