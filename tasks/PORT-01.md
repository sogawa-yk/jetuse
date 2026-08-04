# タスク: PORT-01 公開 ORM スタックの可搬性修正（plan / apply / 初回配信の環境依存）

## 目的
FIX-47 のコードベース監査（2026-07-10）で確定した、**別テナンシへの deploy が plan/apply 時
または初回起動・配信で壊れる infra 側の環境依存**を排除する。Issue #47 と同種の
「自環境でだけ動く」問題の infra 面での根治。

## 事前調査で確定済みの事実（2026-07-10 監査。file:line 検証済み・再検証不要）
- **ocir_namespace の誤誘導**: 既定値は開発テナンシの公開レジストリ namespace で正しい
  （variables.tf:102-106 — 公開イメージの cross-tenancy pull に必須）が、
  `infra/orm/schema.yaml:136-140` が「= Object Storage namespace（tenancy固有）」と説明し
  required 扱い → 誠実な外部デプロイヤほど**自分の namespace を入れて image 404**
  → Container Instance / Functions が起動せずアプリ全滅（plan エラーなし・サイレント）。
- **region_guard は OCIR 可用性しか見ない**（infra/orm/main.tf:7-14・locals.tf:14 =
  kix/nrt/iad/ord）: GenAI（推論 + agentic API）の対応リージョンはより狭く、実証済みは
  **kix（大阪）と ord（Chicago）のみ**（docs/tips.md）。nrt/iad へ deploy すると apply は
  綺麗に通るが GenAI が全滅。README は 4 リージョン対応と主張している。
- **SPA PAR の絶対期限 2027-12-31 固定**（modules/object-storage/variables.tf:12 と
  modules/spa-bucket/variables.tf:12、main.tf の time_expires で消費）→ その日以降の新規
  deploy は**最初から失効した PAR** で SPA 配信が 403（時限爆弾）。
- **ADB のバージョン/サイズ固定**: db_version "26ai"・ECPU 2・LICENSE_INCLUDED がモジュール
  固定で ORM から変更不可（modules/adb）。新規テナンシは ADB ECPU 枠 0 が普通で apply が
  LimitExceeded で落ちる。26ai が提供されないリージョン/レルムでも落ちる。
- **identity-domain の home_region = var.region**（modules/identity-domain/main.tf:7）:
  deploy リージョン ≠ テナンシのホームリージョンだと domain 作成が落ちうる（enable_auth=true
  時のみ）。destroy は local-exec + oci CLI 依存（同 main.tf:16-25）で RM ランナーでの認証が
  未保証 → destroy 失敗 → 同 prefix 再デプロイが衝突。
- **CI shape `CI.Standard.E4.Flex` 固定**（modules/container-instance/main.tf:9）: E4 が無い
  リージョンで apply 失敗の可能性。
- **METRICS_NAMESPACE 未配線**（settings 既定 "jetuse_dev" のまま別テナンシに出る）。
  semantic-store は IAM（DG+policy）だけ作られ実体と SEMSTORE_OCID 配線が無い（死に statement。
  enable_semantic_store 既定値の確認込み）。
- 参考（正しく可搬なもの・変更不要）: SPA は相対 `/api/*` + 実行時 config.json で焼き込みなし。
  package-orm-stacks.sh は git 管理 TF + dist のみで秘密の同梱なし。Functions の同一リージョン
  OCIR 制約は locals の自動導出で解決済み（ADR-0011/0017）。LOG_OCID は配線済み。

## 仕様参照
specs/00、docs/tips.md（リージョン可用性・project はリージョン別）、
docs/guides/branching-and-releases.md、ADR-0011 / ADR-0017（OCIR とリージョン）

## 前提（依存タスク / 人間の事前作業）
- 依存: **FIX-47 が done**（jetuse:test の RM スタック `jetuse-spike-fix47` と承認済み IAM を
  E2E で再利用する）。
- base ブランチ: **main**（`BASE_BRANCH=main`）。
- 人間ゲート: IAM 変更を伴う apply の承認（変更が IAM に及ぶ場合のみ）・コミット / PR / push。

## 対象 area
infra（infra/orm・infra/terraform/modules）。loop-config の area 定義外のため、
`terraform validate` + `terraform fmt -check` + jetuse:test への RM plan 成功を test/lint 相当
として完了条件に明示する。**packages/api には触れない**（PORT-02 と衝突させない）。

## 作業内容
1. **ocir_namespace の説明修正**: schema.yaml の説明を「JetUse 公開イメージのレジストリ
   namespace。**通常は変更しない**（イメージを自テナンシへミラーした場合のみ上書き）」に改め、
   required から外す（グルーピングも Advanced/Hidden 寄りへ）。
2. **GenAI リージョンガード**: region_guard に GenAI 検証済みリージョン（kix/ord）の
   precondition を追加。未検証リージョンは新変数 `allow_unvalidated_genai_region=true` で
   明示オプトイン（エラーメッセージに「GenAI/agentic API の提供状況を確認せよ」と根拠を書く）。
   README・schema.yaml の対応リージョン記述を実態（アプリ=4 リージョン、GenAI 検証済=2）に修正。
3. **SPA PAR の相対期限化**: 絶対日付既定をやめ、apply 時刻起点（例: `timeadd(timestamp(), "8760h")`
   か time_offset リソース）へ。object-storage / spa-bucket の両モジュール。plan 毎の差分暴発を
   避ける実装（ignore_changes 等）に注意。
4. **ADB 変数の公開**: db_version / ecpu_count を ORM スタック変数として公開（既定は現行値）。
   schema.yaml に「ADB ECPU のサービス枠を事前確認（新規テナンシは枠 0 が普通）」を明記。
5. **identity-domain**: home_region をテナンシのホームリージョンから導出（providers.tf の
   home 導出と同一の式）。destroy の CLI 依存は実挙動を確認し、最低限 schema/docs に手動
   デアクティベート手順を明記（enable_auth=true の場合の注意として）。
6. **CI shape の変数化**（既定 E4.Flex 維持）。
7. **配線の穴埋め**: METRICS_NAMESPACE を api_environment へ（prefix 由来の値）。semantic-store
   の IAM 死に statement は enable_semantic_store ゲートの実効性を確認し、SEMSTORE_OCID 変数の
   追加（説明付き）か statement 整理のどちらかに倒す（比較して小さい方 — 実体の自動作成はしない）。
8. **前提チェックリスト**: 外部デプロイヤ向けに「リージョン購読 / ADB・CI・Functions・VCN の
   サービス枠 / GenAI 対応リージョン / OCIR はそのまま」のチェックリストを README（または
   docs/guides）へ。リージョン可用性の実測値は docs/tips.md に追記。

## 完了条件（検証可能な述語で）
- `terraform validate`（infra/orm と変更した全モジュール）成功・`terraform fmt -check` 差分なし。
- jetuse:test（`jetuse-spike-fix47` スタック）への RM plan 成功、変更部分の apply 成功
  （IAM を伴う場合は人間承認後）。
- codex-review の review_verdict=PASS。
- 下記 E2E シナリオ通過・証跡を runs/<run-id>/e2e/ に記録。

## E2E シナリオ（完了ゲート・min_scenarios=2 以上）
1. **PAR 相対期限**: 変更 apply 後、新規発行 PAR の time_expires が apply 時刻起点で将来日付に
   なっていることを CLI で確認し、gateway 経由の SPA 配信が 200 であること。
2. **リージョンガード**: `region=ap-tokyo-1` 指定の plan が明示エラーになり、
   `allow_unvalidated_genai_region=true` で通ること。kix/ord は従来どおり通ること（plan のみで可）。
3. **schema 妥当性**: 変更後 schema.yaml で `oci resource-manager stack update` が受理される
   （Console スキーマとして valid）こと。
（ADB 変数・CI shape は plan 差分ゼロ（既定値時）を確認 — 挙動不変の回帰確認として証跡に含める）

## 成果物
- infra/orm（schema.yaml / variables.tf / main.tf / locals.tf）、
  infra/terraform/modules/{object-storage, spa-bucket, adb, identity-domain, container-instance, iam}
- README / docs/guides の前提チェックリスト、docs/tips.md 追記
- docs/verification/port01-orm-portability.md（E2E 証跡サマリ）

## 禁止事項
- 認証情報・テナンシ/コンパートメント OCID 実値・エンドポイント実値のコミット
- `jetuse-spike-` プレフィックス以外のリソース削除、jetuse:dev / jetuse:public の既存リソース変更
- OCIR `:latest` タグへの push、IAM の無承認 apply、コミット / PR / push の無承認実行
- loop-config.yml・スキル・hooks の編集（仕組みの人間ゲート）
