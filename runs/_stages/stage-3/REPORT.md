# ステージ報告: stage-3（コネクタ＋Platform API ブローカー）

**統合ブランチ:** `feat/stage-3`（base=`feat/loop-engineering` / **未 push・未 PR**）
**生成:** 2026-06-27 / stage-runner（Wave 1＋2 完了）
**結果サマリ:** 完了 **6/6**・blocked 0・残ハードゲート 2（base への PR・push／実 Slack E2E）

> 採用戦略（施主選択）= 「ADR-0014 を先に確定」→ 承認後に残りを自走。全6タスクを `feat/stage-3` へ
> 自動統合し、ステージ完了。**push / base への PR / 実 Slack 投入は未実施**（人間ゲート）。

## 1. タスク別結果
| タスク | status | review_verdict | E2E | 証跡 | 備考/残ゲート |
|---|---|---|---|---|---|
| PAPI-01 | done | PASS（review-4: 0/0/1） | spike 4シナリオ＋監査＋直接SELECT | `jetuse-loops/PAPI-01/runs/2026-06-27T0220_PAPI-01/e2e/` | ADR-0014 採用済。migration 019→020 リナンバ |
| CON-01 | done | PASS（review-8: 0/0/2） | 2＋冪等 | `jetuse-loops/CON-01/runs/2026-06-27T0221_CON-01/e2e/` | connector kind 昇格（spec §12） |
| PAPI-02 | done | PASS（review-6: 0/0/0） | 2run（作成＋冪等） | `jetuse-loops/PAPI-02/runs/2026-06-27T0403_PAPI-02/e2e/` | migration 021・§13 昇格・発行粒度=呼び出しごと確定 |
| PAPI-03 | done | PASS（review-4: 0/**2**/0） | **7シナリオ** | `jetuse-loops/PAPI-03/runs/2026-06-27T0456_PAPI-03/e2e/` | /platform/* ルート。残 major 2（下記§4） |
| CON-02 | done | PASS（review-4: 0/**1**/1） | 2（**mock**・実Slack SKIPPED） | `jetuse-loops/CON-02/runs/2026-06-27T0403_CON-02/e2e/` | **実 Slack 未投入**。残 MAJ-001/MIN-001（§4） |
| CON-03 | done | PASS（review-2: 0/0/0） | 2（broker経由invoke・mock） | `jetuse-loops/CON-03/runs/2026-06-27T0544_CON-03/e2e/` | synth/governance/broker 組込。デモ品質要確認 |

> review_verdict は Codex 採点（0/0/0 = blocker/major/minor）。PASS だが major 残のあるもの（PAPI-03=2, CON-02=1）は §4 に記載。

## 2. 統合差分（base 比 / `feat/stage-3`）
**41 files, +7345/-18**（全て api / specs / spikes / tasks。**web 変更なし** → web build は base 同等）。主な追加:
- **Platform API ブローカー（PAPI-01..03）**: `platform_broker.py`（発行/検証/scope強制/テナント境界/fail-closed/監査）、
  `platform_grants.py`（承認＋発行フロー）、`service/routes/platform.py`（/platform/* = rag.search/db.query読取/conversations/files/connector.invoke）、
  migration `020_platform_broker_audit.sql`・`021_platform_scope_grants.sql`、spec §13。
- **コネクタ（CON-01..03）**: `kind: connector`（manifest）、`connector_store.py`、`connector_runtime.py`（invoke 層: 認可→秘密解決→dispatch・fail-closed）、
  `slack_connector_builtin.py`／`core_connectors.py`（コア Slack）、`synth.py`＋`governance.py` への connector 束縛/パレット検証、
  migration `019_connector_instances.sql`、spec §12.6。
- ADR: `docs/decisions/ADR-0014-platform-api-authorization.md`（**採用**）。
- migration 採番: `019`(connector)/`020`(broker audit)/`021`(grants) — 衝突解消済（PAPI-01 を 019→020 リナンバ）。
- `git -C ../jetuse-loops/_stage-3 diff --stat feat/loop-engineering...feat/stage-3` で確認可。

## 3. 残ハードゲート（人間の承認が必要な事項）
- [ ] **base への PR / push**（このステージ全体。stage-runner は未実施）。
- [ ] **実 Slack E2E**（CON-02/CON-03 は mock で検証済。実 Slack ワークスペースのトークン投入後に再 E2E）。
- [ ] デモ品質確認（CON-03: ヒアリング→コネクタ付きデモ→broker 経由 invoke の一気通貫を人間が確認）。

## 4. 繰越findings（PASS だが残る指摘・次タスク候補）
- **CON-02 MAJ-001**: `connector_runtime._default_mcp_caller` が Responses MCP tool spec で**強制ツールを束縛せず**、
  action 実行がモデル判断依存。決定的 invoke（`invoke_connector_action(...,"send",...)`）の担保が弱い。
  → **実 Slack/MCP 接続時（実 Slack E2E）に forced-tool 束縛で対処**。mock 単体は通過。
- **CON-02 MIN-001**: E2E spike の `_fake_resolver` が secretRef を検証せず常にダミー返却 → secretRef 解決の裏づけが弱い。
- **PAPI-03 残 major 2**: review-4 で blocker0・PASS だが major 2 残（証跡は `jetuse-loops/PAPI-03/runs/.../reviews/review-4.json`）。
  base への PR 前に内容確認を推奨。
- **web 未実装**: CON-03 の「構成プレビューにコネクタ表示」は web 変更を伴わず（API 側データのみ）。
  既存プレビューが connector を表示するか、フロント生成（S5/FE-01）での扱いを要確認。

## 5. コンフリクト/逸脱の記録
- **統合衝突 1件**: `specs/16-platform.md`（PAPI-02 が §13、CON-02 が §12.6 を同位置に追記）。
  **doc-only の追記衝突**（コード衝突なし）のため、`conflict_policy` のサブエージェント解決は用いず
  **オーケストレータが直接 §12.6→§13 の順に並べ替えて解決**。両節が各ブランチ版と**バイト一致**（§13=HEAD と diff 空、
  §12.6=CON-02 と区切り空行1行のみ差）であることを検証してからマージコミット（`ae7d013`）。
  → 軽微・透明な逸脱として本報告で提示。統合後 795 passed / ruff clean で回帰なしを確認。
- **環境**: 統合 worktree `_stage-3` は `bootstrap-env.sh` で専用 venv を作成、`jetuse_registry`（PLG-04）も editable install
  して全件検証（`test_central_registry.py` 等の未インストール落ちは base でも同様の既知ギャップ、CON とは無関係）。
- **loop ADB**: 各タスクの実環境 E2E は共有 loop ADB を `JETUSE_<task>` スキーマで隔離して再利用。PAPI-02/CON-01 で
  ADMIN パスワード再設定（jetuse-dev E2E 専用・memory 方針どおり。各 worktree の e2e/APPROVAL.md 参照）。

## 6. 統合後の最終検証（`feat/stage-3`）
- `.venv/bin/ruff check packages/api` → All checks passed
- `.venv/bin/pytest packages/api/tests` → **795 passed**（coverage 73.4%）
- web 変更なし → web build は base 同等（影響なし）

## 7. 次アクション（人間が承認したら）
1. `feat/stage-3` をレビュー（差分・各 E2E 証跡・spec §12.6/§13・ADR-0014）。特に PAPI-03 残 major 2 と spec 衝突解決を確認。
2. 承認 → base（`feat/loop-engineering`）への PR / push を人間が実施 → CI green 確認 → マージ。
3. **実 Slack E2E**: テスト用 Slack トークン投入後、CON-02 MAJ-001（forced-tool 束縛）を対処して再 E2E。
4. 後始末: `end-loop.sh <task>`（各タスク worktree）＋ `git worktree remove ../jetuse-loops/_stage-3`（統合 worktree）。
   ※ 後始末すると各 worktree の `runs/<id>/e2e/` 証跡も消えるため、必要なら base マージ後に証跡を別途退避。
