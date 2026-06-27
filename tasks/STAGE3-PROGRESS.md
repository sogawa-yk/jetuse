# ステージ3 進捗キュー（loop-runner / stage-runner の単一の真実源）

コネクタ＋Platform API ブローカー（経路2のデモが、DB認証情報を持たずにテナントデータ／SaaS へ到達する基盤）。
`loop-runner` / `stage-runner` が依存順に消化する。status を更新するのは runner（人間がゲートを通した後 ／
stage-runner では Codex PASS＋自動統合後）。
詳細は各 `tasks/<id>.md`、索引は [`README-demo-platform-s3.md`](README-demo-platform-s3.md)、
親計画は [`../docs/enhance/202607-demo-platform-plan.md`](../docs/enhance/202607-demo-platform-plan.md) §7/§6/§9/§10。

status: `todo` | `in_progress` | `blocked` | `done`

前提: ステージ2 完了（HBD-01..05 = done）。ステージ1 完了（PLG-01..08 / SBA-01..04 done。SBA-05 のみ MM-01(VLM) 待ちで blocked）。

| 順 | タスク | 依存 | 人間ゲート | status |
|---|---|---|---|---|
| 1 | PAPI-01 Platform API ブローカー設計ADR＋スパイク | ステージ2 | ADR-0014 承認 / スパイク | done（ADR-0014 採用・feat/stage-3 統合・migration 020 へリナンバ） |
| 2 | CON-01 コネクタ(L2 MCP)モデル＋manifest | ステージ1(PLG-01) | コミット / spec昇格(connector章) | done（feat/stage-3 統合・PASS） |
| 3 | PAPI-02 スコープ承認＋短期トークン発行 | PAPI-01 | コミット / spec昇格(§7) | done（feat/stage-3 統合・migration 021・§13昇格） |
| 4 | PAPI-03 Platform API 実装（5スコープ） | PAPI-02 | コミット | done（feat/stage-3 統合・/platform/* ルート・7 E2E） |
| 5 | CON-02 Slackコネクタ（コア） | CON-01 | Slack 実認証情報の投入 | done（feat/stage-3 統合・mock E2E。実Slackは残ゲート） |
| 6 | CON-03 合成への組込＋E2E | PAPI-03, CON-02, HBD-03/04 | デモ品質 | done（feat/stage-3 統合・broker経由invoke E2E・mock） |

> 並行可: 起動直後は **PAPI-01 と CON-01 が相互独立で並行可（最大2）**。
> 続く波で **PAPI-02（←PAPI-01）と CON-02（←CON-01）が並行可**。
> PAPI-03 は PAPI-02 後。CON-03 は **PAPI-03＋CON-02＋（S2 の HBD-03/04）** が揃ってから（S3 の出口）。
> 単一セッションの loop-runner は「依存が満たされた todo の先頭」を1つずつ実行する。

> **実行方式の選択**:
> - `loop-runner`: 波ごとに人間ゲート（コミット/PR/承認）で停止するセミオート。
> - `stage-runner`（`.claude/loop/start-stage.sh stage-3`）: ステージ承認ループ。PASS タスクを
>   `feat/stage-3` へ自動統合して波を繋ぎ、**ステージ完了で1回だけ報告**。この方式では status 更新は
>   **Codex PASS＋自動統合後**に行い、ADR 承認/Slack 認証投入/デモ品質/push/PR/apply は
>   ステージ報告（`runs/_stages/stage-3/REPORT.md`）に集約して人間に提示する。

## 実行可能集合（開始時）
- PAPI-01 と CON-01（相互独立）。PAPI-01 完了で PAPI-02 解禁、CON-01 完了で CON-02 解禁。

## 人間ゲート（停止して承認を待つ）
- コミット / PR / push（全タスク共通）
- **ADR-0014 承認**: PAPI-01（Platform API 認可モデル）
- spec 昇格: PAPI-02 着手時に §7 を `specs/16-platform.md` へ／CON-01 着手時に connector 章を追記
- **Slack 実認証情報の投入**: CON-02（テスト用 Slack ワークスペース）
- デモ品質: CON-03（ヒアリング→コネクタ付きデモ起動の一気通貫を人間確認）

## ガバナンス（§4 の4制約を弱めない）
固定リファレンス基盤（触らせない）／制約付きパレット（コネクタはコア=Slack 1本に限定）／
合成バリデーション（CON-03 で connector も対象に）／越境防止（Platform API ブローカー経由・DB資格情報を渡さない）。

## 実行ログ（runner が追記）
- 2026-06-27 ステージ3 起票: ステージ2 完了（HBD-01..05 done）を確認し、
  202607-demo-platform-plan.md §7/§10 を基に PAPI-01..03 ＋ CON-01..03 を `tasks/` へ落として本キューを作成。
  ADR-0014 は §11 で予約済（PAPI-01 で起票）。既定の `PLATFORM_SCOPES`（specs/16-platform.md §4）を正本とする。
- 2026-06-27 Wave 1（stage-runner / 方式B 並列）: PAPI-01 と CON-01 を並行自走。
  - CON-01: review-8 PASS（blocker0/major0）・680 tests・ruff clean・実環境E2E 2本+冪等 PASS →
    `feat/stage-3` へ自動統合（merge 9673e1f）。統合後の再検証も 680 passed / ruff clean。status=done。
  - PAPI-01: review-4 PASS（blocker0/major0/minor1）・642 tests・ruff clean・spike E2E 済。ADR-0014 は
    **ドラフトのみ**で停止（hard_gate=adr_approval）。status=blocked（ADR-0014 承認待ち。**未統合**）。
  - 既知の統合課題: PAPI-01 の migration が `019_platform_broker_audit.sql` で CON-01 の
    `019_connector_instances.sql` と採番衝突 → PAPI-01 統合時（Wave 2）に **020 へリナンバ**する。
  - 報告: `runs/_stages/stage-3/REPORT.md`。ADR-0014 承認後に Wave 2（PAPI-02→03 / CON-02→03）。
- 2026-06-27 ADR-0014 採用（施主承認・3点追記）。PAPI-01 を統合（migration 019→020 リナンバ）。
- 2026-06-27 Wave 2（stage-runner / 方式B 並列）: PAPI-02・CON-02 →（PAPI-03）→ CON-03 を自走・統合。
  - PAPI-02: review-6 PASS（blocker0/major0/minor0）・E2E 2run → 統合（migration 021・§13 昇格）。
  - CON-02: review-4 PASS（blocker0/major1/minor1・E2E sufficient）→ 統合。**mock E2E**（実Slack=SKIPPED）。
    残課題: MAJ-001（_default_mcp_caller が MCP forced-tool 未束縛）・MIN-001（E2E secretRef 未検証）。
  - PAPI-03: review-4 PASS（blocker0/major2/minor0・7 E2E シナリオ）→ 統合（/platform/* ルート）。
  - CON-03: review-2 PASS（blocker0/major0/minor0・E2E 2 sufficient）→ 統合（synth/governance/broker経由invoke）。
  - 統合衝突1件: specs/16-platform.md（PAPI-02 §13 ↔ CON-02 §12.6）。**doc-only 追記衝突**のため §12.6→§13 に
    並べ替えて解決、両節が各ブランチとバイト一致であることを検証してマージ（コード衝突なし）。
  - 全6タスク done。feat/stage-3 = 795 passed / ruff clean。残ゲート: push/PR（ステージ全体）・実Slack E2E・繰越findings。
  - ステージ完了報告: `runs/_stages/stage-3/REPORT.md`。
