# 検証: PORT-01 — 公開 ORM スタックの可搬性修正

対象: `infra/orm` と `infra/terraform/modules/{object-storage, spa-bucket, adb, identity-domain, container-instance}`。
FIX-47 監査（2026-07-10）で確定した「別テナンシ deploy が plan/apply/初回配信で壊れる infra 側の環境依存」を根治する。

証跡: `runs/2026-07-13T0730_PORT-01/e2e/`

## 完了条件の対応

| 条件 | 状態 | 証跡 |
|---|---|---|
| (1) `terraform validate` + `fmt -check -recursive` クリーン | ✅ | orm/spa-bucket/dev すべて `Success! The configuration is valid.` / `fmt` 差分なし |
| (2) jetuse:test への RM plan 成功 | ✅ | 未コミット config を zip 化し jetuse-spike RM スタックで plan job **SUCCEEDED**（`scenario3-summary.txt`。Plan: 165 to add, 0 change, 0 destroy, エラー0）|
| (2) 変更部分 apply 成功 | ✅ | 共有スタック apply job SUCCEEDED（131 add/2 change/130 destroy。IAM 変更0・ADB置換0を plan hard gate で確認）。apply 後 fix_wallet_and_restart.sh 実行→DB READY |
| (3) codex review_verdict=PASS | ✅ | review-2 PASS（blocker 0）。証跡込み最終レビューで確定 |
| (4) E2E シナリオ ≥2 | ✅ | Scenario 1（PAR相対+gateway200・実apply後）+ Scenario 2 + Scenario 3（`e2e/DONE.md`）|

## 作業内容の対応（tasks/PORT-01.md §作業内容 1–8）

1. **ocir_namespace 誤誘導の修正**: schema.yaml の説明を「JetUse 公開イメージの namespace。通常変更不要」に改め
   `required: false` へ。variables.tf のコメントも「Object Storage namespace とは無関係」に訂正。グルーピングは
   「オプション（詳細・通常変更不要）」末尾へ移動。さらに **region_guard に「公開既定以外に変更するなら
   api_image_url/fn_router_image を両方必須」の precondition を追加**（codex review-1 F-001 対応。namespace 単独
   変更でのサイレント image 404 を plan 時に停止）。→ `ocir-namespace-guard.txt` で deny/pass を実証。
2. **GenAI リージョンガード**: `region_guard` に第2 precondition を追加（kix/ord のみ許可、未検証は
   `allow_unvalidated_genai_region=true` で明示オプトイン）。README/schema.yaml を「アプリ=4リージョン／
   GenAI 実証済=2リージョン」の2階層記述に修正。→ **Scenario 2 で実証**。
3. **SPA PAR 相対期限化**: object-storage / spa-bucket 両モジュールで `spa_par_expiry` 空時に
   `time_offset`（hashicorp/time、offset_years=1）で apply 時刻起点+1年を state に固定。base を保持する
   ため plan 毎の差分は出ず、`ignore_changes` を使わないので明示指定時の後からの変更も反映される
   （codex review-1 F-002 対応）。→ RM plan v2 で `hashicorp/time v0.14.0` 導入・`time_offset.spa_par` 計画・
   PAR `time_expires=(known after apply)` を確認。既存固定日付スタックは初回 apply で PAR 1回再発行。
4. **ADB 変数の公開**: `adb_db_version`(26ai) / `adb_ecpu_count`(2) を ORM 変数として公開（既定は現行値）。
   schema.yaml に ECPU サービス枠の事前確認を明記。
5. **identity-domain home_region**: テナンシのホームリージョンから導出（`local.home_region`、providers.tf の
   home alias と同式）してモジュールへ渡す。空フォールバックで後方互換。destroy の手動デアクティベート手順を
   `docs/guides/customize.md` に明記。
6. **CI shape 変数化**: `ci_shape`（既定 CI.Standard.E4.Flex）。
7. **配線の穴埋め**: `METRICS_NAMESPACE = replace(prefix,"-","_")`（既定 "jetuse_dev" 固定を解消）、
   `SEMSTORE_OCID = var.semstore_ocid` を api_environment へ。semantic-store の IAM は `enable_semantic_store`
   で既に有効ゲート済（死に statement は gate 有効）。実体は作らず OCID 変数追加に倒した（比較して小さい方）。
8. **前提チェックリスト**: README にデプロイ前チェックリスト、docs/guides/customize.md に可搬性の変数表と
   Identity Domain destroy 注意、docs/tips.md にリージョン2階層の実測値を追記。
- **追加（tips 2026-07-13 が PORT-01 タグ）**: `prefix` の validation（英小文字始まり・sanitized≤15）と
  ADB wallet の `replace_triggered_by=[db_name]`（rename 時の stale wallet 根治）。

## E2E シナリオ結果

### Scenario 2 — リージョンガード（`scenario2-summary.txt` / `regionguard-*.out`）
ローカル `terraform plan -target=terraform_data.region_guard`（テナンシは NRT 購読済＝OCIR ガードは tokyo を通す
ので、発火するのは新 GenAI ガードのみ）:

- `region=ap-tokyo-1` → **DENY**（GenAI precondition failed、エラー文に allow_unvalidated_genai_region の案内）
- `region=ap-tokyo-1` + `allow_unvalidated_genai_region=true` → **PASS**
- `region=ap-osaka-1`(kix) / `us-chicago-1`(ord) → **PASS**（従来どおり）

### Scenario 3 — schema 妥当性 + RM plan（`scenario3-summary.txt`）
未コミット config を zip 化し jetuse:test に `jetuse-spike-port01-schema` スタックを作成 → **受理（schema valid）**。
plan job **SUCCEEDED**（Plan: 165 to add, 0 change, 0 destroy、precondition/null エラー 0）。検証後スタック削除済み。

### 回帰 / 入力検証（`prefix-validation.txt`）
`prefix` validation をフル plan で確認: `jetuse-spike-47`(共有)/`jetuse`=PASS、`jetusetoolongname12`(19>15)/
`Jetuse`(大文字始まり)=FAIL。ADB/CI shape は既定値のため挙動不変（RM plan で新規作成として現行値どおり）。

### Scenario 1 — PAR 相対期限 + gateway 200（共有スタック apply 後・実施済 ✅）
`fix47-e2e-shared/PORT-01-APPLY-OK` 出現後に共有スタック `jetuse-spike-fix47` を config-source 更新
（既存変数は保持＝PORT-02 の port02-e2e イメージも維持）→ 変更部分を apply。
- PAR `jetuse-spike-47-spa-read`: created=2026-07-13T10:12:41Z / **expires=2027-07-13T10:12:40Z**
  ＝apply 時刻起点 +1年（相対）。旧固定 2027-12-31 ではない。
- gateway 経由 SPA: `/` `/index.html` `/config.json` すべて **200**。
- apply 直後に `fix_wallet_and_restart.sh APPLY_JOB=<id>` を実行（wallet 再生成→CI 再起動→DB READY）。
証跡: `e2e/scenario1-summary.txt` / `scenario1-{apply,walletfix,par.json,gateway}.txt`。
