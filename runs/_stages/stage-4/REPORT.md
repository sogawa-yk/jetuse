# ステージ報告: stage-4（コンテナデプロイ＋マーケット拡張）

**統合ブランチ:** `feat/stage-4`（base=`feat/loop-engineering` / **未 push・未 PR**）
**生成:** 2026-06-27 / stage-runner（Wave 1＋2 完了）
**結果サマリ:** done **4/5**・deferred 1（ASSET-01）・残ハードゲート（ADR-0016 承認／terraform apply 複数／base PR）

> 採用戦略（施主選択）= 「ADR-0015 を先に確定」＋「ASSET-01 は後回し（外部資産専用パス）」。
> stage 4 は **apply/billing 依存が濃い**ため、自走では「設計＋IaC plan/validate＋コード＋mock/loop-ADB E2E＝PASS」まで進め、
> **実 apply を要する E2E は SKIPPED に明記**してここに集約。push / base への PR / apply は未実施（人間ゲート）。
> ADR-0015 は施主承認済（2点追記）。DEP-02 が新たに **ADR-0016（提案中）** を起票＝承認ゲートが1つ増えた。

## 1. タスク別結果
| タスク | status | review_verdict | E2E | 証跡 | 備考/残ゲート |
|---|---|---|---|---|---|
| MKT-01 | **done** | PASS（review-4: 0/0/0） | 2（mock レジストリ） | `jetuse-loops/MKT-01/runs/2026-06-27T0905_MKT-01/e2e/` | sample-app/connector 流通拡張。実レジストリ apply 残 |
| MKT-02 | **done** | PASS（review-4: 0/0/0） | 2（loop ADB, sufficient） | `jetuse-loops/MKT-02/runs/2026-06-27T1048_MKT-02/e2e/` | registry μService（ADB・評価・DL数・版・検索）。μService apply 残 |
| DEP-01 | **done** | PASS（review-21: 0/0/1） | 4（仕様生成＋tf plan/validate） | `jetuse-loops/DEP-01/runs/2026-06-27T0905_DEP-01/e2e/` | ADR-0015 採用・統合済。実 L3 apply 残 |
| DEP-02 | **done** | PASS（review-3: 0/0/1） | 2（mock コンテナ→Platform API, sufficient） | `jetuse-loops/DEP-02/runs/2026-06-27T1159_DEP-02/e2e/` | トークン注入＋ライフサイクル。**ADR-0016 承認**＋実コンテナ apply 残 |
| ASSET-01 | **deferred** | — | — | — | 外部資産専用パスへ後回し（memory `stage4-asset01-deferred.md`） |

## 2. 統合差分（base 比 / `feat/stage-4`）
**46 files, +4785/-281**（MKT-01/02 ＋ DEP-01/02）。検証: api **902 passed** / registry **108 passed** / ruff clean / `terraform validate` Success。
- **MKT-01**: publish/install/scaffold を sample-app/connector kind に拡張。
- **MKT-02**: `packages/registry` を backend 抽象＋ADB バックエンド（評価/DL数/版/検索）へ昇格、migration `022_plugin_registry.sql`。後方互換・ed25519 署名維持。
- **DEP-01**: `jetuse_core/deploy.py`（デモ構成→コンテナ仕様の決定的・fail-closed マッピング、秘密二重遮断）＋ `infra/terraform/environments/hosted-demo/`（既存 container-instance を consume・**plan/validate のみ**）＋ ADR-0015。
- **DEP-02**: `jetuse_core/deploy_inject.py`（コンテナ起動時の `issue_token`→ベースURL＋短期トークン注入、DB 資格情報を渡さない、失効/更新、承認スコープに厳密）＋ hosted-demo terraform 拡張 ＋ **ADR-0016**（注入/ライフサイクル）。
- migration 採番: `022`（MKT-02）のみ追加（stage-3 の 019-021 に続き衝突なし）。

## 3. 残ハードゲート（人間の承認が必要な事項）
- [ ] **ADR-0016 承認**: L3 デモの Platform API 注入＋配備ライフサイクル（更新/破棄/命名規約）。`docs/decisions/ADR-0016-l3-demo-platform-injection-lifecycle.md`（提案中）。ADR-0015 §7 が「実 apply 前に確定」と要求＝**実 apply の前提**。
- [ ] **terraform apply・課金**（全て plan/mock 止まり。apply は人間判断）:
  - DEP-01/02: hosted-demo コンテナの実 apply（実 L3 配備）＋ OCIR への実イメージ push
  - MKT-01: 実レジストリ（Object Storage バケット）流通の apply
  - MKT-02: registry μService の実デプロイ apply
- [ ] **base への PR / push**（このステージ全体。stage-runner 未実施）。
- ※ ADR-0015 は承認済（追記反映済）。

## 4. deferred
- **ASSET-01**: 伝ぴょん/No.1-RAG/SQL-Assist は外部資産・SSO・接続が人間ゲート濃いため後日の専用パス
  （memory `stage4-asset01-deferred.md`。ADR-0015 §8 に従い着手時に追補 ADR）。

## 5. コンフリクト/逸脱の記録
- **自動統合の衝突: なし**（MKT-01/02・DEP-01/02 とも clean merge）。
- ADR-0015 をレビューで承認（status=採用、2点追記: §7 ライフサイクル＝実apply前にDEP-02確定 / §8 ASSET-01 追補ADR）。
- 秘密遮断は deploy.py ＋ Terraform variable validation の **二重**（env キー名前空間 allowlist・秘密名ヒント拒否・Vault OCID 値拒否）を実装で確認。
- **環境**: stage worktree `_stage-4` は専用 venv ＋ `jetuse_registry` editable。loop ADB は `JETUSE_<task>` スキーマ隔離で再利用。

## 6. 次アクション（人間が承認したら）
1. `feat/stage-4` と **ADR-0016** をレビュー（DEP/MKT 差分・E2E 証跡・注入/ライフサイクル設計）。
2. **ADR-0016 を承認** → 実 apply の前提が整う。
3. **apply 方針の決定**（課金）: L3 配備・実レジストリ・μService のどれをいつ apply するか。apply 後に実 E2E を再実施。
4. base への PR / push（承認後・CI green 確認後）。
5. ASSET-01 は別途「外部資産専用パス」で起こす（他ステージ完了後）。
