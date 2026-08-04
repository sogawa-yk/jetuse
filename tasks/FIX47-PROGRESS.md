# Issue #47 可搬性修正 進捗キュー（loop-runner / stage-runner の単一の真実源）

Issue #47（別テナンシで RAG アップロード 500）の根治と、コードベース監査（2026-07-10）で確定した
「他テナンシーで起こる環境依存」の排除。**base=`main`**（Public 変更 — infra/orm・packages/api・
iam モジュール。`docs/guides/branching-and-releases.md`）。タスク起動時は `BASE_BRANCH=main` を渡す。

- **E2E 環境**: 施主指示（2026-07-10）により **jetuse:test コンパートメントに実リソースを作成**して
  実施（loop-config の e2e.compartment=jetuse-dev をこのキューに限り上書き）。RM スタック
  `jetuse-spike-fix47` を 3 タスクで共有し、**キュー全体の完了後に destroy**（後始末参照）。
- **人間ゲート**: IAM（DG/Policy）apply の承認・コミット / PR / push・Issue #47 コメント投稿・
  OCIR `:latest` への push（正規リリースは release.yml）。

status: `todo` | `in_progress` | `blocked` | `done`

| 順 | タスク | 依存 | 人間ゲート | status |
|---|---|---|---|---|
| 1 | [FIX-47 RAG 500 根治 + PROJECT_OCID / IAM 可搬性](FIX-47.md) | — | IAM apply 承認 + コミット（両方 済） | done |
| 2 | [PORT-01 ORM スタックの plan/apply 可搬性](PORT-01.md) | FIX-47 | （IAM を伴う場合 apply 承認）+ コミット | todo |
| 3 | [PORT-02 ランタイム縮退（機能別環境依存の表面化）](PORT-02.md) | FIX-47 | コミット | todo |

> 第1波 = FIX-47（クリーンルーム再現・PROJECT_OCID 配線・IAM 最小権限・E2E 基盤の構築）。
> 第2波 = PORT-01 ∥ PORT-02（相互独立: PORT-01 は infra のみ・PORT-02 は api のみに閉じる —
> ファイル衝突なし・並列可）。
>
> **2026-07-13 第1波 done**: FIX-47 は codex PASS（review-3・実ブラウザ live check 込み）+
> test/lint 緑 + jetuse:test 実環境 E2E 4シナリオ合格（再現/クリーンルーム/明示 PROJECT_OCID/
> ネガティブ — runs/2026-07-10T0842_FIX-47/e2e/RESULTS.md）。コミット済（証跡内の実 OCID/
> エンドポイントはマスクの上でコミット）。**残=Issue #47 コメント投稿（キュー級の人間ゲート）**。
> IAM は施主の方針変更により
> スタック外へ: 既存 DG jetuse-deploy-test-dg + 手動 policy（iam-report.md が正本）で
> enable_dynamic_group=false / enable_runtime_policy=false。E2E リージョンは大阪 VCN 枠超過で
> us-chicago-1 に変更（チケットの事前承認範囲）。スタック jetuse-spike-fix47 は PORT-01/02 の
> E2E で再利用するため残置（後始末はキュー完了後 — 自動作成 project jetuse-project 含む）。
> コミット後に done → 第2波（PORT-01 ∥ PORT-02 並列）へ。

## 監査サマリ（2026-07-10。詳細・file:line は各チケットの「事前調査で確定済みの事実」）

- **CRITICAL**: PROJECT_OCID 未配線 — RAG だけでなく**既定チャットモデル・会話メモリも全滅**
  （FIX-47）／ GenAI 未検証リージョン（nrt/iad）へ無警告 deploy 可（PORT-01）
- **HIGH**: ocir_namespace の schema 説明が誤誘導（自 namespace を入れると image 404 で全滅、
  PORT-01）／ ADB 26ai・ECPU 固定で新規テナンシの apply が枠不足で失敗（PORT-01）／
  モデル可用性・semantic store 未構成・Select AI RP・Speech/OCR の縮退不全（PORT-02）
- **MEDIUM**: SPA PAR が 2027-12-31 固定の時限爆弾（PORT-01）／ TTS の Phoenix 購読前提が不可視
  （PORT-02）／ obs「oci log ship failed」spam（PORT-02）／ identity-domain home_region・
  destroy の CLI 依存（PORT-01）
- **LOW**: CI shape 固定・METRICS_NAMESPACE 未配線・semstore の死に IAM（PORT-01）／
  AUTH_MODE 未設定時の未処理クラッシュ（PORT-02）
- **可搬と確認済み（対処不要）**: SPA（焼き込みなし・相対パス + 実行時 config.json）、
  ORM zip（秘密の同梱なし）、Functions の同一リージョン OCIR（自動導出済み）、LOG_OCID 配線、
  translate/guardrails/moderation/OpenSearch/VPD/hosted-agent の縮退

## キュー完了条件

- 3 タスクすべて codex-review PASS・test/lint（infra は validate/fmt/plan）クリーン・
  jetuse:test 実環境 E2E（または理由付き SKIPPED）通過。
- クリーンルーム（GenAI project ゼロ・手動セットアップなしの jetuse:test）で
  「deploy → 既定モデル chat → RAG upload → file_search 応答 → dbchat 応答」が通る。
- Issue #47 への返信コメント案（修正内容 + 自己診断手順）が起草済み（投稿は人間ゲート）。

## 後始末（最終タスクの E2E 完了後）

- RM スタック `jetuse-spike-fix47` を destroy（jetuse-spike- プレフィックスのみ。
  自動作成された GenerativeAiProject・専用タグの OCIR イメージも削除）。
- jetuse:test に残存リソースが無いことを CLI で確認し、証跡を最終タスクの runs/<run-id>/e2e/ に残す。
