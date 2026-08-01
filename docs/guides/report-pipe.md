# 報告パイプ（人間向け HTML の置き場を各自の環境へ逃がす）

ループが出す**人間向け報告書（HTML）をどこへ置くか**を、リポジトリではなく各開発者のホーム側
（`~/.claude/skills/`）スキルに委ねるための契約。決定の背景は `docs/decisions/ADR-0018-human-report-pipe.md`。
設定の単一真実源は `loop-config.yml` の `report:` ブロック。

## なぜ分けるか

報告書は**仕様でも証跡でもなく「読み物」**である。読む人が普段ドキュメントを読む場所（施主の場合は
Obsidian vault）に置かれるべきで、リポジトリに閉じる理由がない。一方で「報告書に何をどう書くか」は
全員で揃える必要がある。したがって責務を次で切る。

| 責務 | 持ち主 | 実体 |
| --- | --- | --- |
| 報告の**様式** | リポジトリ | `.claude/skills/loop-protocol/references/task-packet-template.html`、`.claude/skills/stage-runner/references/stage-report-template.md` |
| 報告の**中身**（何を書くか・例外だけ露出） | リポジトリ | `loop-protocol`「人間ゲートに出すタスクパケット」 |
| 判定の**証跡** | リポジトリ | `runs/<run-id>/`（reviews / e2e / goal.txt） |
| 報告書の**置き場の解決と配置** | **各自のホーム（`~/.claude`）** | 個人スキル（既定 `preview`） |

## 契約（インターフェース）

ループ側は、埋め終えた**単一ファイル HTML** を `report.build_dir`（= `runs/<run-id>/report/<TASK>.html`）
に書き出したうえで、個人スキルを**配置モード**で呼ぶ。

**入力**
- 完成済み単一ファイル HTML の**絶対パス**
- `topic` 名（`loop-config.yml` の `report.topic`）。**`<YYYY-MM-DD>_<内容がわかるタイトル>`**。
  タスクIDをファイル名にしない（`RP-01.html` では中身が分からない。IDは本文の脚注に残す）
- `purpose`（`reports` = 読み物 / `decisions` = 承認を問うもの）。**承認・可否・選択を1つでも
  問うているなら `decisions`**。迷ったら `decisions` に倒す（未処理の判断が埋もれるのを防ぐ）
- モード: `place_only`

**個人スキルがすべきこと**
1. 出力先を解決する（このリポジトリの規約ではリポジトリ直下の `.obsidian-dir` の1行目＝対象フォルダ）。
2. `<対象フォルダ>/_renders/<purpose>/<topic>.html` へ**そのまま配置**する
   （サブディレクトリが無ければ作る）。
3. 絶対パスと `![[<topic>.html]]`（Obsidian 埋め込み記法）を返す。
   **埋め込みはファイル名で解決するので、ディレクトリを分けてもリンクは切れない。**

**個人スキルがしてはいけないこと**
- **HTML を再生成・改変しない。** 様式はリポジトリ側が持つ。要約し直す・体裁を整える・章立てを変えるのは
  すべて契約違反（全員の報告が揃わなくなる）。配置と出力先解決だけを行う。

**未設定時（fallback）**
個人スキルが無い / `.obsidian-dir` が無い場合、ループは `report.fallback`（既定 `artifact`）に従い
`build_dir` の HTML をそのまま Artifact 化して提示し、「報告パイプ未設定」を1行添えて**続行する**。
報告パイプはループの完了ゲートではない。

## 各自のセットアップ（新しく参加した人向け）

1. ホーム側に配置スキルを1本用意する。既存の `preview` 系スキルがあるなら、
   「完成済み HTML を渡されたら再生成せず配置する」分岐を足すだけでよい。
2. スキル名が `preview` 以外なら、起動時に `LOOP_REPORT_SKILL=<name>` を渡す
   （`loop-config.yml` は書き換えない＝個人差をリポジトリに持ち込まない）。
3. 出力先を決める。このリポジトリ直下に `.obsidian-dir`（gitignore 済み）を作り、1行目に対象フォルダの
   絶対パスを書く。ループ起動スクリプト（`start-loop.sh` / `begin_stage.sh`）が worktree へ複製する
   （gitignore 済みファイルは `git worktree add` で伝播しないため）。
4. 何もしない選択も可。その場合は fallback（Artifact 提示）で報告を受け取る。

## 注意

- **HTML はリポジトリにコミットしない**（`.gitignore` の `runs/**/*.html` / `docs/verification/*.html`）。
- 報告書の置き場が変わっても、**判定の根拠は常にリポジトリ側の `runs/<run-id>/`** にある。
  監査・`loop-doctor` の診断はそちらを読む。
