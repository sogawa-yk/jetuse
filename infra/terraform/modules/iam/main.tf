# 注意: 本モジュールのapplyはIAM変更のため人間承認必須(CLAUDE.md)。
# エージェントユーザーにはテナンシ権限がなく404になるため enable_iam=false が既定。
# 人間がコンソールで作成する場合の手順は docs/setup/iam.md(本定義と同期を保つこと)。
#
# テナンシの動的グループ/ポリシー数制限のため、3リソースタイプを
# 1動的グループ + 1ポリシーに統合(2026-06-10ユーザー指示)。
# 権限は3プリンシパルの和集合になる — Phase 8で最小権限分割を再検討。

resource "oci_identity_dynamic_group" "app" {
  compartment_id = var.tenancy_ocid
  name           = "${var.prefix}-dg"
  description    = "JetUse: SemanticStore enrichment + CI/Functions resource principals"
  matching_rule  = <<-EOT
    Any {all {resource.type='generativeaisemanticstore', resource.compartment.id='${var.compartment_ocid}'},
         all {resource.type='computecontainerinstance', resource.compartment.id='${var.compartment_ocid}'},
         all {resource.type='fnfunc', resource.compartment.id='${var.compartment_ocid}'}}
  EOT
}

resource "oci_identity_policy" "app" {
  compartment_id = var.compartment_ocid
  name           = "${var.prefix}-policy"
  description    = "JetUse runtime permissions (SQL Search + app, compartment scope)"
  statements = [
    "Allow dynamic-group ${oci_identity_dynamic_group.app.name} to use generative-ai-family in compartment id ${var.compartment_ocid}",
    "Allow dynamic-group ${oci_identity_dynamic_group.app.name} to use database-tools-family in compartment id ${var.compartment_ocid}",
    "Allow dynamic-group ${oci_identity_dynamic_group.app.name} to read database-family in compartment id ${var.compartment_ocid}",
    "Allow dynamic-group ${oci_identity_dynamic_group.app.name} to use autonomous-database-family in compartment id ${var.compartment_ocid}",
    "Allow dynamic-group ${oci_identity_dynamic_group.app.name} to read secret-family in compartment id ${var.compartment_ocid}",
    "Allow dynamic-group ${oci_identity_dynamic_group.app.name} to manage objects in compartment id ${var.compartment_ocid}",
    # API Gateway が Functions(fn-router)を呼び出すための許可(presets/dbchat/tts セグメント)
    "Allow any-user to use functions-family in compartment id ${var.compartment_ocid} where ALL {request.principal.type = 'apigateway', request.resource.compartment.id = '${var.compartment_ocid}'}",
  ]
}
