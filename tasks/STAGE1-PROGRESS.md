# ステージ1 進捗キュー（stage-runner の単一の真実源）— SP1: JetUse API

デモ生成プラットフォーム再設計（`specs/17-demo-platform-redesign.md` / ADR-0015）の第一ステージ＝
**SP1: JetUse API**（能力カタログ + DemoContext seam + デモスコープ縦切り）。
**base=`main`**（SP1 は Public 共通の土台 — specs/17 §7）、ステージ統合ブランチ `feat/sp1-jetuse-api`。
PASS したタスクを stage-runner がステージブランチへ自動 commit+merge する。push / main への PR /
apply / IAM は自走中も停止（人間ゲート）。

status: `todo` | `in_progress` | `blocked` | `done`

| 順 | タスク | 依存 | 人間ゲート | status |
|---|---|---|---|---|
| 1 | [SP1-01 能力ディスクリプタ8件 + GET /api/capabilities](SP1-01.md) | — | コミット | done |
| 2 | [SP1-02 demos 最小レジストリ + DemoContext seam](SP1-02.md) | — | コミット | done |
| 3 | [SP1-03 デモスコープ能力ルート縦切り（chat + rag）](SP1-03.md) | SP1-01, SP1-02 | コミット | done |

> 第1波 = SP1-01 ∥ SP1-02（相互独立・並列可）。第2波 = SP1-03。
> 8能力（chat/rag.search/dbchat/agents/voice/minutes/translate/docunderstand）のルートはすべて main に
> 実在する（translate/docunderstand は `routes/voice.py` 内）ため、ルート新設タスクは無い。

## ステージ完了条件（specs/17 §9。ステージ報告で人間が確認）

- 3タスクすべて Codex review PASS・test/lint クリーン・実環境 E2E（または理由付き SKIPPED）通過。
- `GET /api/capabilities` が 8 能力のカタログ（OpenAPI 由来技術詳細 + 手書きディスクリプタ）を返す。
  裏方ルート（admin/conversations/tools 等）は載らない。
- `/api/demos/{demo_id}/...` 配下が `DemoContext` を経由し、**他ユーザーのデモ id では 404** になる
  （所有権検証 fail-closed の実機確認）。Public 用 user 単位ルートは回帰なし。
- `main` が常時デプロイ可能（既存テスト・`npm run build` 回帰なし）。

## スコープ境界（specs/17 §8）

- Demo エンティティの本格 CRUD・箱のプロビジョニング（スキーマ/ベクタストア生成）は **SP2**。
  SP1-02 の demos テーブルは所有権検証に必要な最小列のみ（specs/17 §9 の受け入れ条件が根拠）。
- `connector.invoke`・統一 Capability インターフェース（案2）・ビルダー・マーケットは対象外。

## 実行ログ（stage-runner が追記）
- 2026-07-06: 第1波起動（SP1-01 ∥ SP1-02、herdr 方式B・専用ペイン・LOOP_AUTONOMOUS=1・base=feat/sp1-jetuse-api）。
- 2026-07-06: SP1-01 done — Codex PASS（blocker 0・major 1 residual=example の model キー3箇所）・225 passed・ruff 緑・E2E 証跡 runs/2026-07-06T0117_SP1-01/e2e/。feat/sp1-jetuse-api へ統合（2238dd8）、統合後 test/lint 緑。※統合時に .current_run_id が混入→ 9cb325f で除去し .gitignore 復元（integrate_task.sh の除外リスト改善は loop-doctor 案件としてステージ報告に記載）。
- 2026-07-06: SP1-02 done — Codex review-2 PASS（review-1 FAIL: 証跡内 ADB OCID 露出→マスク・VARCHAR2 CHAR 化で解消）・230 passed・ruff 緑・実 ADB E2E 2シナリオ（スキーマ JETUSE_SP1_02 隔離、証跡 runs/2026-07-06T0117_SP1-02/e2e/）。統合後 test/lint 緑。※旧 loop ADB が旧コンパートメントに STOPPED 残存（権限不足で削除不能→人間ゲート）、新 ADB jetuseloop2 を jetuse/dev に Terraform で再作成（e2e/APPROVAL.md 参照）。
- 2026-07-06: 第2波起動（SP1-03、base=feat/sp1-jetuse-api=SP1-01+02 統合済み断面）。
- 2026-07-06: SP1-03 done — Codex review-2 PASS（review-1 FAIL: 公開デモ書込ゲート欠落等 blocker 3 → require_demo_owner・422ガード・実RS256トークンE2E再実施で解消）・242 passed・ruff 緑・実環境 E2E 3シナリオ（jetuseloop2/JETUSE_SP1_03、file_search は大阪 vector store 枠逼迫のため us-chicago-1 で実施）。統合後 test/lint 緑。residual major 3件（REV-007/008/009）はステージ報告でトリアージ提示。
- 2026-07-06: キュー枯渇 → ステージ報告 runs/_stages/sp1-jetuse-api/REPORT.md を作成し停止（人間ゲート）。
