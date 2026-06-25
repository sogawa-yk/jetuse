# LOOP — ループエンジニアリングの使い方（クイックスタート）

実装＝Claude Code（maker）／レビュー＝Codex（checker）／停止採点＝`/goal` の三層ループ。
現在は **Stage 1 (report-only)**：実装とレビュー・履歴記録までを自動化し、**コミット/PR/push は人間承認後**。

> 詳細・設計の根拠: [`docs/loop-engineering.md`](docs/loop-engineering.md) ／ [`docs/decisions/ADR-0012-loop-engineering.md`](docs/decisions/ADR-0012-loop-engineering.md)
> 運用ルール本体: [`CLAUDE.md`](CLAUDE.md) の「ループエンジニアリング」節

---

## 3ステップで回す

### 1. タスクを書く
`tasks/_template.md` を複製して `tasks/<task>.md` を作る。受け入れ条件は**検証可能な述語**で書く。

```bash
cp tasks/_template.md tasks/<task>.md
$EDITOR tasks/<task>.md
```

### 2. loop モードで Claude を起動
`LOOP_TASK` を付けて起動したセッションだけ、履歴記録の hooks が発火する
（未設定の通常セッションは完全 no-op で影響なし）。

```bash
LOOP_TASK=<task> GOAL="$(cat <<'EOF'
tasks/<task>.md の受け入れ条件をすべて満たし、かつ
(1) <該当areaのtest_cmd> が全件パス、(2) 該当 area の lint がクリーン、
(3) STATE.md の review_verdict が PASS であること。
未承認のコミット・PR・push は一切行わない。
EOF
)" claude
```

- 任意で `CODEX_MODEL=...` を前置（未指定なら codex 既定モデル）。
- 起動時に `runs/<日時>_<task>/`（manifest・goal・turns・diffs・reviews）が採番される。
- test_cmd / lint_cmd は `loop-config.yml` の `areas.{web,api}` を参照。

### 3. セッション内で `/goal` を実行
上の `GOAL` と同じ完了条件を `/goal` に登録すると、条件が真になるまでループが回る。
完了条件には**必ず「STATE.md の review_verdict が PASS」を含める**（停止判定に外部レビューを結ぶ）。

---

## 毎ターン何が起きるか（`loop-protocol`）

```
STATE.md を読む → 最小差分で実装 → codex-review で Codex にレビューさせる
 → runs/<id>/reviews/ と STATE.md を更新 → FAIL は次ターンで修正
```

- 採点者は **Codex**。Claude は `review_verdict` を自分で PASS に書き換えない。
- `blocker` が1件でもあれば FAIL。`major`/`minor` は記録されるが停止条件は塞がない。

---

## 困ったら → `loop-doctor`

成果物やループ運用に問題を感じたら `loop-doctor` スキルに渡す。コードではなく
**ループの仕組み**（スキル・/goal 条件・hooks・設定）を、`runs/` の履歴を根拠に診断・修正提案する。
編集は**承認後のみ**。

| 症状 | 渡す先 |
| --- | --- |
| 同じ指摘が再発 / レビューが甘い・過剰 / 終わらない・空回り / トークン浪費 | `loop-doctor` |
| 履歴が記録されない / 空の run が出る | `loop-doctor`（hooks の `LOOP_TASK` ガード点検） |

---

## 人間ゲート（必ず承認が要る）

- コミット / PR / push / リリース
- `loop-doctor` による仕組みの編集
- Stage 引き上げ（report-only → auto-fix → auto-commit）
- 破壊的操作・アクセス権変更・認証情報入力（設計上行わない）
