# ステージ報告: <stage-id>

**統合ブランチ:** `feat/<stage-id>`（base=`feat/loop-engineering` / **未 push・未 PR**）
**生成:** <ISO日時> / stage-runner
**結果サマリ:** 完了 <done数>/<総数>・blocked <数>・残ハードゲート <数>

> 人間チェック用。`feat/<stage-id>` を確認 → 承認後に base への PR/push を人間が実施する。
> 各タスクの詳細レビューは HTML パケット（`docs/verification/<TASK>.html`）で行う。本報告は索引＋例外の集約。

## 0. ⚠ 判断が要る事項（最上段・無ければ「なし」）
例外を表の1セルに埋めない。override / 未対応 residual / 後続未起票を、どのタスクの何かが分かる形で列挙:
- [ ] **<TASK>**: Codex FAIL を override 統合（`review-N`・迂回した blocker/major の要約・理由）→ 承認可否
- [ ] **<TASK>**: 未対応 residual（`F-00X: file:line`・受容根拠）→ 受容可否
- [ ] **<TASK>**: 後続タスク未起票 — <内容>

## 1. タスク別結果
| タスク | status | verdict | E2E | パケット(HTML) |
|---|---|---|---|---|
| HBD-01 | done | PASS | n/n 通過 | `docs/verification/HBD-01.html` |
| … | | | | |

## 2. 統合差分（base 比）
- 変更ファイル数 / 主な追加・変更点（タスク横断で何が入ったか）。
- `git -C <stage-worktree> diff --stat feat/loop-engineering...feat/<stage-id>` の要約。

## 3. 残ハードゲート（人間の承認が必要な事項）
- [ ] base への PR / push（このステージ全体）
- [ ] ADR 承認: <ADR番号・論点>（ドラフトは作成済み: パス）
- [ ] terraform apply・課金: <該当タスク>（plan まで実施・apply 未）
- [ ] IAM/Identity: <該当>（人間手動）
- [ ] デモ品質チェック: <SBA/HBD タスク>（デモ起動手順・確認観点）
- [ ] 統合 blocked（衝突解決不能 等）: <タスク・理由>

## 4. コンフリクト/逸脱の記録
- 自動統合で発生した衝突と、その解決（サブエージェント解決→Codex 再レビュー結果）。
- ハードゲートで blocked にしたタスクと理由。

## 5. 次アクション（人間が承認したら）
1. `feat/<stage-id>` をレビュー（差分・E2E 証跡・デモ品質）。
2. 承認 → base への PR 作成 / CI green 確認 / マージ。
3. 後始末: `end-loop.sh <task>`（各タスク worktree）＋統合 worktree 撤去。
