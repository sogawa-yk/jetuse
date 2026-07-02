# JetUseの実行時プリンシパルは責務ごとに分離する。
# 呼び出し元stackは実行者の権限と既存IAMに応じて、作成範囲を個別に切り替える。

locals {
  runtime_dynamic_group_name        = "${var.prefix}-runtime-dg"
  adb_dynamic_group_name            = "${var.prefix}-adb-dg"
  semantic_store_dynamic_group_name = "${var.prefix}-semantic-store-dg"
}

resource "oci_identity_dynamic_group" "runtime" {
  count = var.enable_dynamic_group ? 1 : 0

  compartment_id = var.tenancy_ocid
  name           = local.runtime_dynamic_group_name
  description    = "JetUse Container Instances and Functions resource principals"
  matching_rule  = <<-EOT
    Any {all {resource.type='computecontainerinstance', resource.compartment.id='${var.compartment_ocid}'},
         all {resource.type='fnfunc', resource.compartment.id='${var.compartment_ocid}'}}
  EOT
}

resource "oci_identity_dynamic_group" "adb" {
  count = var.enable_dynamic_group ? 1 : 0

  compartment_id = var.tenancy_ocid
  name           = local.adb_dynamic_group_name
  description    = "JetUse Autonomous Database resource principal"
  matching_rule  = "All {resource.type='autonomousdatabase', resource.compartment.id='${var.compartment_ocid}'}"
}

resource "oci_identity_dynamic_group" "semantic_store" {
  count          = var.enable_dynamic_group && var.enable_semantic_store ? 1 : 0
  compartment_id = var.tenancy_ocid
  name           = local.semantic_store_dynamic_group_name
  description    = "JetUse OCI Generative AI semantic store resource principal"
  matching_rule  = "All {resource.type='generativeaisemanticstore', resource.compartment.id='${var.compartment_ocid}'}"
}

locals {
  runtime_statements = [
    # Chat / Responses / Projects / Guardrails / hosted agent invocation.
    "Allow dynamic-group ${local.runtime_dynamic_group_name} to use generative-ai-family in compartment id ${var.compartment_ocid}",
    # RAG の Vector Store と Files はアプリが作成・削除するため manage が必要。
    "Allow dynamic-group ${local.runtime_dynamic_group_name} to manage generative-ai-vectorstore in compartment id ${var.compartment_ocid}",
    "Allow dynamic-group ${local.runtime_dynamic_group_name} to manage generative-ai-vectorstore-file in compartment id ${var.compartment_ocid}",
    "Allow dynamic-group ${local.runtime_dynamic_group_name} to manage generative-ai-file in compartment id ${var.compartment_ocid}",
    # ADB wallet 取得、RAG/議事録ファイル、AIサービス、可観測性。
    "Allow dynamic-group ${local.runtime_dynamic_group_name} to use autonomous-database-family in compartment id ${var.compartment_ocid}",
    "Allow dynamic-group ${local.runtime_dynamic_group_name} to manage objects in compartment id ${var.compartment_ocid}",
    "Allow dynamic-group ${local.runtime_dynamic_group_name} to read buckets in compartment id ${var.compartment_ocid}",
    "Allow dynamic-group ${local.runtime_dynamic_group_name} to manage ai-service-speech-family in compartment id ${var.compartment_ocid}",
    "Allow dynamic-group ${local.runtime_dynamic_group_name} to use ai-service-document-family in compartment id ${var.compartment_ocid}",
    "Allow dynamic-group ${local.runtime_dynamic_group_name} to use ai-service-language-family in compartment id ${var.compartment_ocid}",
    "Allow dynamic-group ${local.runtime_dynamic_group_name} to read tag-namespaces in compartment id ${var.compartment_ocid}",
    "Allow dynamic-group ${local.runtime_dynamic_group_name} to use log-content in compartment id ${var.compartment_ocid}",
    "Allow dynamic-group ${local.runtime_dynamic_group_name} to use metrics in compartment id ${var.compartment_ocid}",
    # 事前作成された MCP credential secret を読む場合に使用。secret の作成権限は付与しない。
    "Allow dynamic-group ${local.runtime_dynamic_group_name} to read secret-family in compartment id ${var.compartment_ocid}",
    # API Gateway から Functions ルーターを呼び出す。
    "Allow any-user to use functions-family in compartment id ${var.compartment_ocid} where ALL {request.principal.type = 'ApiGateway', request.resource.compartment.id = '${var.compartment_ocid}'}",
  ]

  adb_statements = [
    # DBMS_CLOUD_AI / Select AI が ADB の resource principal で推論・RAGを行う。
    "Allow dynamic-group ${local.adb_dynamic_group_name} to use generative-ai-family in compartment id ${var.compartment_ocid}",
    "Allow dynamic-group ${local.adb_dynamic_group_name} to read objects in compartment id ${var.compartment_ocid}",
  ]

  semantic_store_statements = var.enable_semantic_store ? [
    "Allow dynamic-group ${local.semantic_store_dynamic_group_name} to use database-tools-family in compartment id ${var.compartment_ocid}",
    "Allow dynamic-group ${local.semantic_store_dynamic_group_name} to read secret-family in compartment id ${var.compartment_ocid}",
    "Allow dynamic-group ${local.semantic_store_dynamic_group_name} to read database-family in compartment id ${var.compartment_ocid}",
    "Allow dynamic-group ${local.semantic_store_dynamic_group_name} to read autonomous-database-family in compartment id ${var.compartment_ocid}",
    "Allow dynamic-group ${local.semantic_store_dynamic_group_name} to use generative-ai-family in compartment id ${var.compartment_ocid}",
  ] : []
}

resource "oci_identity_policy" "runtime" {
  count = var.enable_runtime_policy ? 1 : 0

  compartment_id = var.compartment_ocid
  name           = "${var.prefix}-runtime-policy"
  description    = "JetUse least-privilege runtime permissions"
  statements = concat(
    local.runtime_statements,
    local.adb_statements,
    local.semantic_store_statements,
  )

  depends_on = [
    oci_identity_dynamic_group.runtime,
    oci_identity_dynamic_group.adb,
    oci_identity_dynamic_group.semantic_store,
  ]
}

# Object Storage namespace はテナンシ単位のため、コンパートメントポリシーとは分離する。
# Dynamic GroupをTerraformで作成する場合に一緒に作成する。
# enable_dynamic_group=false の場合は、既存Dynamic Groupと共に事前作成済みであることを前提とする。
resource "oci_identity_policy" "runtime_tenancy" {
  count = var.enable_dynamic_group ? 1 : 0

  compartment_id = var.tenancy_ocid
  name           = "${var.prefix}-runtime-tenancy-policy"
  description    = "JetUse runtime tenancy-level read-only permission"
  statements = [
    "Allow dynamic-group ${local.runtime_dynamic_group_name} to read objectstorage-namespaces in tenancy",
  ]

  depends_on = [oci_identity_dynamic_group.runtime]
}

# 任意の既存グループを JetUse 専用コンパートメントのデプロイ担当にする。
# all-resources は必ず専用コンパートメントに限定し、テナンシ管理権限は付与しない。
resource "oci_identity_policy" "deployer" {
  count          = var.create_deployer_policy ? 1 : 0
  compartment_id = var.tenancy_ocid
  name           = "${var.prefix}-deployer-policy"
  description    = "Allow a non-tenancy-admin group to deploy JetUse with OCI Resource Manager"
  statements = [
    "Allow group ${var.deployer_group_subject} to inspect compartments in tenancy",
    "Allow group ${var.deployer_group_subject} to inspect tenancies in tenancy",
    "Allow group ${var.deployer_group_subject} to read objectstorage-namespaces in tenancy",
    "Allow group ${var.deployer_group_subject} to manage orm-stacks in compartment id ${var.compartment_ocid}",
    "Allow group ${var.deployer_group_subject} to manage orm-jobs in compartment id ${var.compartment_ocid}",
    "Allow group ${var.deployer_group_subject} to manage all-resources in compartment id ${var.compartment_ocid}",
  ]

  lifecycle {
    precondition {
      condition     = trimspace(var.deployer_group_subject) != "" && !strcontains(var.deployer_group_subject, "\n")
      error_message = "deployer_group_subject must identify an existing OCI IAM group (for example Default/JetUseDeployers)."
    }
  }
}

# enable_iam=true で作成済みのstateを、新しいcount付きリソースへ移行する。
moved {
  from = oci_identity_dynamic_group.runtime
  to   = oci_identity_dynamic_group.runtime[0]
}

moved {
  from = oci_identity_dynamic_group.adb
  to   = oci_identity_dynamic_group.adb[0]
}

moved {
  from = oci_identity_policy.runtime
  to   = oci_identity_policy.runtime[0]
}

moved {
  from = oci_identity_policy.runtime_tenancy
  to   = oci_identity_policy.runtime_tenancy[0]
}
