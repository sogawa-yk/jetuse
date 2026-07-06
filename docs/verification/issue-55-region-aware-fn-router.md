# 検証: Issue #55 — 非Osakaリージョンでの fn-router 自動無効化（ADR-0017）

日付: 2026-07-06 / 環境: dev インスタンス（実テナンシ、Terraform 1.15）

## 変更点

`infra/orm/locals.tf`: デプロイリージョンのキーを既存の
`data.oci_identity_region_subscriptions`（providers.tf でホームリージョン導出に使用済み）から導出し、
`ocir_region_key` と不一致なら fn-router の既定イメージを空にする（= router 非作成、
API Gateway の catch-all で CI にフォールバック）。明示指定 `fn_router_image` は常に優先。

## 実施した検証

1. **`terraform validate` / `terraform fmt -check`（infra/orm）**: 通過。
2. **リージョンキー導出と無効化判定（実データソース）**: locals.tf と同一式を実テナンシの
   `oci_identity_region_subscriptions` に対して評価:

   | region | deploy_region_key | fn_router_default |
   |---|---|---|
   | ap-osaka-1 | `kix` | `kix.ocir.io/<ns>/jetuse-fn-router:latest`（従来どおり作成） |
   | ap-tokyo-1 | `nrt` | 空（router 非作成） |

   ※ `region_key` の実値は大文字（`KIX`/`NRT`）で返るため `lower()` 必須（実測）。
3. **空→非作成→ルート無しの経路**: 既存実装で担保
   （`modules/functions/main.tf` の `count = var.router_image == "" ? 0 : 1`、
   `infra/orm/main.tf` の `fn_routes = router_function_id == "" ? {} : {...}`、
   api-gateway の catch-all `/api/{p*}` → CI）。specs/02-infra §api-gateway
   「空ならルート生成しない」の既定義挙動。

## 制約（未実施）

- **infra/orm のフル `terraform plan` はローカルでは完走不可**（変更と無関係の既存事象）:
  `data.oci_objectstorage_namespace`（compartment_id 指定の GetNamespace）がローカルユーザー権限で
  `CompartmentIdNotFound` となり namespace が null → object_storage 依存の
  CI / router / API GW deployment が plan から脱落する。CLI 実測で
  `oci os ns get --compartment-id <ocid>` も同エラー（引数なしは成功）。
  Resource Manager（デプロイヤ権限）では従来から成功している（docs/verification/orm-jetuse-apply.md）。
- **ap-tokyo-1 での実 apply**: RM 経由のワンクリック実行が必要（人間ゲート）。
  Issue #55 の失敗点 `CreateFunction` 自体が生成されなくなるため、同エラーの再発はない。
