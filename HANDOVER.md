# 引き継ぎメモ — デモ生成プラットフォーム化 ステージ1 実装

**作成:** 2026-06-25 / **対象:** 別セッションでステージ1（PLG-01〜08・SBA-01〜05）を実装する人（=Claude Code ループ）

このメモだけ読めば着手できることを目的とする。詳細は各リンク先を正とする。

---

## 0. 最重要の前提（ブランチ運用）
- **開発のベースブランチ = `feat/loop-engineering`**。実装はすべてこのブランチを基点に行う。
- **main へのマージは「開発がうまく行った時」だけ**。main は安定版。直接 push しない。
- 1タスク = 1ブランチ = 1PR（`feat/loop-engineering` へ向けてPR）。
  **ブランチはタスク開始時にループが自動で切る**（`feat/<task-id>` を base から。`ensure_task_branch.sh`）。
  手で `git checkout -b` する必要はない。流れ:
  ```bash
  # base に居て作業ツリーがクリーンな状態で起動するだけ:
  LOOP_TASK=PLG-01 claude          # session_start が feat/PLG-01 を自動作成→run採番
  # …ループで実装・完了…→ 人間承認後にコミット → feat/loop-engineering へPR
  ```
  - 自動切替は追跡ファイルがクリーンな時のみ（未コミット変更が残っていると中断＝先に前タスクをコミット/マージ）。
  - 依存タスクは依存先が base にマージ済みであること。連鎖する場合は `BASE_BRANCH=feat/<dep>` を前置。
- ステージ1がひととおり通り、実機検証（`docs/verification/`）が揃ったら、`feat/loop-engineering` → main をPRでマージ。

> 注意: ループ基盤（`.claude/` の hooks/skills・`LOOP.md`・`loop-config.yml`・`tasks/`）は `feat/loop-engineering` 上にのみ存在する。**必ずこのブランチ（またはその子）で作業する**こと。main から枝分かれするとループが動かない。

---

## 1. まず読むもの（この順）
1. [`LOOP.md`](LOOP.md) — ループの回し方（Claude×Codex×/goal）。
2. [`docs/enhance/202607-demo-platform-plan.md`](docs/enhance/202607-demo-platform-plan.md) — 計画の正本（v2。§2決定一覧／§4ガバナンス／§9ロードマップ／§10タスク）。
3. [`docs/enhance/202607-hearing-flow.md`](docs/enhance/202607-hearing-flow.md) — S2の素案（S1では参照のみ）。
4. [`tasks/README-demo-platform-s1.md`](tasks/README-demo-platform-s1.md) — タスク索引・依存・実行順。
5. 補助: [`docs/comparison/marketplace-plugin.md`](docs/comparison/marketplace-plugin.md)（方式比較）。

確定した技術判断（D1〜D11）と「2つの利用経路（経路1=輸入／経路2=ビルダー）」は計画書 §1.4・§2 を参照。**ステージ1は経路1（宣言型の公開・インストール）の配管**である。

---

## 2. 実装の回し方（テンプレ）

リポジトリルートで、base(`feat/loop-engineering`)に居て作業ツリーがクリーンな状態で loop モードで起動する（**ブランチ `feat/<task>` は自動で切られる**）:

```bash
LOOP_TASK=PLG-01 GOAL="$(cat <<'EOF'
tasks/PLG-01.md の受け入れ条件をすべて満たし、かつ
(1) .venv/bin/pytest packages/api/tests/test_plugin_manifest.py が全件パス、
(2) .venv/bin/ruff check packages/api がクリーン、
(3) STATE.md の review_verdict が PASS（最新のCodexレビューが合格）
であること。未承認のコミット・PR・push は行わない。
ADR-0013 はドラフトを作成し人間レビューを要求する（承認なしに確定としない）。
EOF
)" claude
```

- セッション内で **`/goal`** に同じ完了条件を登録 → ループ開始。
- 毎ターンは `loop-protocol` に従う: **実装 → `codex-review`（Codexが差分を採点）→ `runs/<id>/` と STATE.md に記録 → FAILは次ターンで修正**。
- **採点者はCodex**。`review_verdict` を自分で PASS に書き換えない。blocker が1件でもあれば FAIL。
- `LOOP_TASK` 付き起動時だけ履歴 hooks が発火し `runs/<日時>_<task>/` が採番される。
- **コミット/PR/push は人間承認後**（現 Stage = report-only。`loop-config.yml`）。

各タスクの GOAL は `loop-config.yml` の `goal_template` ＋ 当該 `tasks/<id>.md` の受け入れ条件で組む。area 別の test/lint コマンドは `loop-config.yml` の `areas`（web=vitest/eslint, api=pytest/ruff）。

### 2.1 一括実行（タスクを順番に回す / `loop-runner`）
1タスクずつ手で launch する代わりに、`loop-runner` スキルでキューを**依存順に逐次消化**できる:

```bash
LOOP_TASK=stage1 claude     # 起動後、セッション内で:
# 「tasks のキューを順番に実施して」と指示（loop-runner スキルが起動する）
```

- 進捗の正は [`tasks/STAGE1-PROGRESS.md`](tasks/STAGE1-PROGRESS.md)。loop-runner が「依存が満たされた todo の先頭」を1つ選び、
  `begin_task.sh` でタスク用 run-id を採番 → `loop-protocol` で実装→`codex-review`→記録、を回す。
- **人間ゲート（コミット/PR・ADR承認・apply・デモ品質・VLM前提）で必ず停止**し、何を承認すれば次へ進めるかを提示する。
  承認後に当該タスクを `done` にして次へ。
- **完全無人にはならない**（Stage=report-only ＋ 人間ゲート）。無人度を上げたい場合は `loop-config.yml` の
  `stage` を `auto-fix`/`auto-commit` に上げる＝**人間の判断**（loop-impl §8）。それでも apply・ADR・デモ品質ゲートは残す想定。
- 1セッション=逐次1本。**並行**したい場合は別セッションを人間が複数立てる（PROGRESS の「並行可」注記参照）。

---

## 3. タスクと推奨実行順
索引は [`tasks/README-demo-platform-s1.md`](tasks/README-demo-platform-s1.md)。

```
PLG-01 → PLG-02 → PLG-03 →（PLG-04 並行可）→ PLG-07
   → SBA-01 → SBA-02 →（PLG-05/06、SBA-03/04/05 を並行）→ PLG-08（出口判定）
```

- 配管: PLG-01 manifest＋ADR-0013 / 02 データモデル / 03 取込＋署名 / 04 中央レジストリ(planまで) / 05 公開 / 06 マーケットUI / 07 ローダー / 08 E2E。
- サンプルアプリ: SBA-01 構造定義 / 02 SBA-A 問い合わせ(RAG) / 03 SBA-B 在庫(NL2SQL) / 04 SBA-C 営業(エージェント複合) / 05 SBA-D 帳票(OCR)。

---

## 4. 人間ゲート（ループが停止して承認を待つ）
- **ADR-0013 承認** … PLG-01（基盤の設計決定）。
- **Terraform apply・課金リソース** … PLG-04（エージェントは `plan` まで）。
- **デモ品質チェック** … SBA-02・PLG-08（ステージ1出口）。
- **VLM/マルチモーダル能力の前提確認** … SBA-05（MM-01 相当が無ければ先行実装の要否を相談）。
- そのほか **コミット/PR/push** は常に人間承認後。

---

## 5. 開始前チェックリスト
- [ ] `git branch --show-current` が `feat/loop-engineering`（またはその子）になっている。
- [ ] `codex --version` が通り、`codex exec` の認証が有効（レビューに必須。`CODEX_MODEL` 未指定なら既定モデル）。
- [ ] Python venv（`.venv`）と `pytest`/`ruff`、Node（`npm`/`vitest`/`eslint`）が使える。
- [ ] `.env` に必要な環境依存値（COMPARTMENT_OCID 等）がある（**コミット禁止**）。
- [ ] 依存の確認: SBA-04 は既存の議事録(VOICE-01)・エージェント(AGT-01..03)、SBA-05 は MM-01(VLM) に依存。着手前に当該機能の有無を確認。

---

## 6. 困ったとき
- レビューが甘い/過剰・同じ指摘が再発・ループが終わらない/空回り・トークン浪費 → **`loop-doctor`** に渡す。`runs/` の履歴を根拠に「ループの仕組み」側の修正案を出す（編集は承認後のみ）。
- 各タスク完了時は **`docs/verification/<id>.md`** に実機検証ログを残す（実機検証主義）。
- 仕様にない判断が要るときは実装せず **ADR 案**（`docs/decisions/`）を書いて人間レビューを要求（spec-driven）。

---

## 7. 完了の定義（ステージ1）
全タスクの受け入れ条件を満たし、PLG-08 で「インスタンス間で宣言型ユースケースを公開→輸入→実行」が実機で成立し、各 `docs/verification/` が揃うこと。ここで `feat/loop-engineering` → main マージを人間が判断する。
