# 2026-06-29 codex-review にライブ Playwright MCP E2E を追加

## 症状（人間要望）
- codex レビュー時に Playwright MCP を使った E2E も実施してほしい。

## 設計判断
- **URL 条件付き**: 到達可能なターゲット URL が与えられたときだけ実施。env `E2E_BASE_URL`
  または `runs/<id>/e2e/target_url.txt`（先頭の非コメント行）で渡す。未提供時は従来どおり
  ブラウザを使わず証跡評価のみ（非 UI レビューの速度・無意味なブラウザ起動を避ける）。
- **独立検証**: checker（Codex）自身が実ブラウザで diff 関連の主要フローを検証。
- **teeth**: 主要フローが壊れていれば Codex は blocker を立て verdict=FAIL。
- read-only sandbox 下でも MCP ツールは動く（シェル実行とは別系統）ため `--sandbox read-only` のまま。

## 変更ファイル
- `.claude/skills/codex-review/scripts/run_codex_review.sh`
  - `E2E_BASE_URL` / `runs/<id>/e2e/target_url.txt` から URL 検出。
  - URL があればライブ E2E 指示を INSTRUCTIONS に追記、payload に TARGET_URL を注入。無ければ
    「ブラウザ不使用・live_check は not_performed」と指示。
- `.claude/skills/codex-review/scripts/review-schema.json`
  - `e2e.live_check`（performed / target_url / result / scenarios[] / notes）を追加（required）。
- `.claude/skills/codex-review/SKILL.md` — ライブ E2E 節を追記。

## 実機テスト（2026-06-29）
- ローカルアプリ（title「JetUse Demo Login」、Login ボタンはハンドラ無し）を起動し
  `E2E_BASE_URL` を渡してレビュー実行。
- 結果: `e2e.live_check.performed=true`, `target_url` 正、2 シナリオ実施。初期表示=pass、
  Login 操作=fail（無反応を正しく検出、favicon 404 も観測）。`result=fail` → blocker → **verdict=FAIL**。
- 仕組み（ブラウザ実行・記録・teeth）が end-to-end で機能することを確認。

## 補足
- codex exec + Playwright MCP は read-only/OnRequest でも MCP tool が auto-run（feasibility 検証済）。
- 公開インターネット一般（example.com 等）は egress 制限で不安定。jetuse-dev は自 IP 限定 LB のため
  この dev インスタンスから到達可能な URL を渡すこと。
