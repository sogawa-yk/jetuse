---
name: loop-runner
description: tasks/STAGE1-PROGRESS.md のキューを依存順に消化する上位ループ。実行可能で相互独立なタスクは Agent Teams で並列に（各エージェントが goal ループで実装→codex-review→記録）、依存があるものは順に回し、人間ゲートで停止する。「タスクを順番に実施して」「キューを回して」等を頼まれたら使う。完全無人ではなくゲートで停止するセミオート。
---
# loop-runner：タスクキューの実行（上位オーケストレータ）

あなたは複数タスクを回す**オーケストレータ**。各タスクの中身は `loop-protocol` / `codex-review` に従う。
進捗の単一の真実源は `tasks/STAGE1-PROGRESS.md`（順序・依存・ゲート・status）。

## 実行可能集合とその性質
**実行可能タスク = 依存がすべて done で status=todo のもの。** STAGE1-PROGRESS の依存定義より、
**実行可能集合の中のタスクは互いに依存しない**（A が B に依存するなら、B が done でない限り A は実行不可）。
よって実行可能集合はそのまま**並列バッチ**にできる。

## 手順
1. `tasks/STAGE1-PROGRESS.md` を読み、**実行可能集合**を求める。空なら終了（全 done か残りは blocked）。
2. **分岐**:
   - 実行可能が **1 つ** → 逐次モード（その1つを「タスク実行契約」で回す）。
   - 実行可能が **2 つ以上** → **並列モード（Agent Teams）**。同時実行は最大 **3**（超過は次の波）。
3. 各タスクの完了後は STAGE1-PROGRESS の status を更新し、1 に戻る。

## 並列モード（Agent Teams）
実行可能集合から最大 3 タスクを選び、**各エージェント＝1タスク**で並列起動する。
worktree 隔離によりファイル・ブランチ・インデックスを共有せず衝突しない。

**起動方式は実行環境で分岐する（`echo $HERDR_ENV` で判定）:**

### 方式B（`HERDR_ENV=1` のとき・推奨）— herdr ペインで可視化起動
herdr 内で回っている場合は、各タスクを**専用ペインで起動した実 claude** として走らせる。
各エージェントが何をしているかが herdr サイドバーに `working`/`blocked`/`done` で見え、
`herdr pane read` でいつでも作業内容を覗ける（`herdr` スキル参照）。

タスクごとに（最大3、`--no-focus` で自分のフォーカスは保つ）:
1. ペインを作る。**main（オーケストレータ）ペインを縮め続けないため、分割の起点を切り替える**:
   - **1本目のタスク**: `<自分のpane>`（main）を右に分割して「タスク列」を作る。生成ペインを `ANCHOR` に記録。
     `PANE=$(herdr pane split <自分のpane> --direction right --no-focus | \
     python3 -c 'import sys,json; print(json.load(sys.stdin)["result"]["pane"]["pane_id"])'); ANCHOR=$PANE`
   - **2本目以降**: main ではなく**直前タスクペイン `$ANCHOR` を下に分割**して右列に積む（main を再分割しない）。
     `PANE=$(herdr pane split "$ANCHOR" --direction down --no-focus | \
     python3 -c 'import sys,json; print(json.load(sys.stdin)["result"]["pane"]["pane_id"])'); ANCHOR=$PANE`
   - これで main は台数に関係なく約 1/2 を維持（最低でも画面の 1/4 を割らない）。タスクペインだけが右列で縦に縮む。
   - `herdr pane rename` 相当が無いので**ペイン内の最初のコマンドでタスク名を表示**しておく
     （例: `herdr pane run "$PANE" "echo '=== <task> ==='"`）。
2. そのペインで worktree 隔離込みの公式ランチャを**自律モードで**起動:
   `herdr pane run "$PANE" "LOOP_AUTONOMOUS=1 GOAL='<完了条件>' .claude/loop/start-loop.sh <task>"`。
   start-loop.sh がタスク専用 worktree を切り、`bypassPermissions`（権限プロンプトで止まらない）かつ
   ハードゲート（コミット/PR/push/merge/apply/destroy は `--disallowedTools` で遮断）で `claude` を起動する。
   - **`LOOP_AUTONOMOUS=1` 必須**: 無人ペインは付けないと毎ツールの承認プロンプトで停止し自走できない。
   - **`GOAL='<完了条件>'` 必須**: session_start.sh が `runs/<id>/goal.txt` に記録する（証跡）。
3. プロンプト待ちを待ってから goal プロンプトを流し込む:
   `herdr wait output "$PANE" --match '>' --timeout 60000` →
   `herdr pane run "$PANE" "<下記『各エージェントへ渡すプロンプト』>"`。
   - **注意**: 大きなプロンプトは入力欄で `[Pasted text]` に畳まれ Enter が消費され未送信になることがある。
     投入後に `herdr pane list` で当該ペインが `working` でなければ `herdr pane send-keys "$PANE" Enter` で送信する。
4. 全ペイン起動後、**完了を待ち合わせて回収**:
   各 `$PANE` について `herdr wait agent-status "$PANE" --status done --timeout <十分大>` →
   `herdr pane read "$PANE" --source recent --lines 120` で最終の構造化メッセージを取得。
   `blocked` で止まったペインは人間ゲート待ちとして拾う。

### 方式A（`HERDR_ENV` 非設定のとき）— Agent ツール
**`isolation: worktree` のサブエージェントを1メッセージで並列起動**する（各エージェント＝1タスク）。
戻り値を集約する。可視化ペインは無いが実行モデルは同じ。

### 各エージェントへ渡すプロンプト（両方式共通）
そのタスクの **goal を回す**指示。**本リポジトリに `/goal` というスラッシュコマンドは存在しない**
（Stop hook `log_turn.sh` はターン記録のみで、goal 採点・再プロンプトはしない）。ループは
**エージェント自身が loop-protocol を毎ターン辿って自走**することで回る。よって完了条件は
（A）起動時の `GOAL` env で `goal.txt` に記録し、（B）下記プロンプトに完了条件を埋めて agent に渡す、
の二点で伝える（スラッシュコマンドは使わない）:
- タスク: `tasks/<id>.md`（受け入れ条件・E2E シナリオ）。base ブランチ: `feat/loop-engineering`
  （依存連鎖時は `feat/<dep>`）。
- 完了条件: `loop-config.yml` の `goal_template` を当該タスクで具体化（test_cmd/area・E2E 含む）。
- 手順: 下記「タスク実行契約」を厳守（実装 → `codex-review` → FAIL は修正 → **review_verdict=PASS** かつ
  test/lint クリーン かつ **実環境 E2E 通過** まで自走）。
- **実環境 E2E の並列隔離**: 共有 loop ADB（`jetuse-loop-adb`）を再利用しつつ、**タスク専用スキーマ
  `JETUSE_<TASK>` で隔離**する（ADMIN で `CREATE USER JETUSE_<task>` → そのスキーマへ migrate/E2E。
  ADB は増やさない）。非 DB タスクは ADB に触れない。
- **コミットしない**。PASS まで実装したら停止し、最終メッセージで {task, review_verdict, e2e結果,
  証跡パス, 残る人間ゲート} を構造化して返す（方式Bでは最終メッセージをペインに出力する）。

オーケストレータは全エージェントの戻り（方式B では各ペインの `pane read` 結果）を集約し、
**人間ゲートをまとめて提示**する（下記ゲートで停止）。
1 波が終わってゲートを人間が通したら、STAGE1-PROGRESS を更新し次の波へ。
方式B のペインは波の確認が済むまで残し、後始末は `.claude/loop/end-loop.sh <task>` と `herdr pane close` で行う。

## 逐次モード / 各エージェントが従う「タスク実行契約」
1. `GOAL="<完了条件>" .claude/skills/loop-runner/scripts/begin_task.sh <task-id>` を実行。
   タスク用ブランチ `feat/<task>` を base から自動で切り、run-id を採番する。
   - 追跡ファイルに未コミット変更が残るとブランチ切替は中断 → 先に手順4のゲートを片付ける。
2. `tasks/<task>.md` の受け入れ条件と `goal_template` から完了条件を組み、STATE.md「現在のタスク」に記入。
3. `loop-protocol` を回す: 実装 → `codex-review` → `runs/<id>/` と STATE.md に記録 → FAIL は次ターンで修正。
   **review_verdict=PASS かつ area の test/lint クリーン かつ 実環境 E2E 通過**まで。verdict は自分で書き換えない。
4. **人間ゲートで必ず停止**（自動で越えない）:
   - コミット / PR / push（全タスク共通）
   - ADR 承認（PLG-01）／ Terraform apply・課金（PLG-04）／ デモ品質（SBA-02, PLG-08）／ VLM 前提（SBA-05）
   「**何を承認すれば次に進めるか**」を明示して待つ。
5. 人間がゲートを通したら STAGE1-PROGRESS の当該タスクを `done` に更新。
6. 依存未達・能力前提なし等で進めないものは status=`blocked` にして理由を書く。

## 原則
- 並列は**実行可能集合（相互独立）に限る**。依存があるものは依存先が base にマージされるまで回さない。
- 同時実行は最大 3。各並列タスクは worktree 隔離（`HERDR_ENV=1` は herdr ペイン＋start-loop.sh、
  それ以外は Agent ツール `isolation: worktree`）／実環境 E2E は `JETUSE_<task>` スキーマで隔離。
- Stage は `loop-config.yml` に従う（既定 report-only ＝ コミットしない）。無人度を上げるのは人間ゲート。
- **完全無人化はしない。ゲートを飛ばさないことがこの仕組みの価値。** 並列でもゲートはタスクごとに必ず止める。

## 使い方の例
`.claude/loop/start-loop.sh stage1`（worktree 起動・推奨）または `LOOP_TASK=stage1 claude` で起動し、
「tasks のキューを回して」と指示するか本スキルを起動する。実行可能が複数あれば自動で並列、なければ逐次。
