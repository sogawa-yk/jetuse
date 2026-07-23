# 検証: Issue #55 — 対応4リージョン固定とOCIRレジストリ自動導出（ADR-0017）

日付: 2026-07-06 / 環境: dev インスタンス（実テナンシ、Terraform 1.15）

## 変更点

- `infra/orm/locals.tf`: レジストリのリージョンキーを既存の
  `data.oci_identity_region_subscriptions`（providers.tf でホームリージョン導出に使用済み）から
  自動導出し、`<region-key>.ocir.io` をイメージ URL に使用。`ocir_region_key` 変数は削除
  （schema 入力からも除去）。明示指定 `api_image_url` / `fn_router_image` は常に優先。
- `infra/orm/main.tf`: `terraform_data.region_guard` — 対応4リージョン（kix/nrt/iad/ord）外は
  plan 時に precondition で明示エラー（両イメージ明示指定時は通過）。
- `.github/workflows/release.yml`: イメージ push 先を OCIR 4リージョン（+GHCR）へ拡張。

## 実施した検証

1. **`terraform validate` / `terraform fmt`（infra/orm）・YAML 構文（schema.yaml / release.yml）**: 通過。
2. **リージョンキー導出（実データソース・5リージョン評価）**: locals と同一式を実テナンシの
   `oci_identity_region_subscriptions` に対して評価（証跡: `runs/2026-07-06T0805_ISSUE-55/e2e/`）:

   | region | 導出キー | 合成レジストリ | ガード |
   |---|---|---|---|
   | ap-osaka-1 | `kix` | `kix.ocir.io/...` | 通過 |
   | ap-tokyo-1 | `nrt` | `nrt.ocir.io/...` | 通過 |
   | us-ashburn-1 | `iad` | `iad.ocir.io/...` | 通過 |
   | us-chicago-1 | `ord` | `ord.ocir.io/...` | 通過 |
   | ap-seoul-1（対応外） | `icn` | —(ガードで停止) | **エラー** |

   ※ `region_key` の実値は大文字（`KIX` 等）で返るため `lower()` 必須（実測）。
3. **ガードの実動作（infra/orm 本体スタックの `terraform plan`・実テナンシ）**:
   - `region=ap-seoul-1` → `Resource precondition failed` が発火し、対応4リージョンと
     ミラー+明示指定の回避策を示すメッセージを plan 時に表示（Issue #55 の「apply 途中で
     不可解に失敗」を plan 時の明示エラーへ置換）。
   - `region=ap-tokyo-1` → ガードエラーなし、`terraform_data.region_guard` 計画済み。

## 制約（未実施）

- **infra/orm のフル `terraform plan` はローカルでは完走不可**（変更と無関係の既存事象）:
  `data.oci_objectstorage_namespace`（compartment_id 指定の GetNamespace）がローカルユーザー権限で
  `CompartmentIdNotFound` となり namespace が null → object_storage 依存の
  CI / router / API GW deployment が plan から脱落する。CLI 実測で
  `oci os ns get --compartment-id <ocid>` も同エラー（引数なしは成功）。
  Resource Manager（デプロイヤ権限）では従来から成功している（docs/verification/jetuse-app/orm-jetuse-apply.md）。
- **release.yml の4リージョン push**: merge 後の Actions 実行で確認（事前に nrt/iad/ord への
  OCIR repo 作成が必要 — ADR-0017「前提となる人間側の作業」）。
- **東京/アシュバーン/シカゴでの RM 実 apply**: 人間ゲート。イメージ push 完了後に実施可能。
