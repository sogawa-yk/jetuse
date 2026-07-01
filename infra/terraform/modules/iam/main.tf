# JetUse の実行時プリンシパルは責務ごとに分離する。
# IAM の作成自体は infra/orm-bootstrap からテナンシ管理者が一度だけ行い、
# 通常のアプリ用 ORM スタックには含めない。

resource "oci_identity_dynamic_group" "runtime" {
  compartment_id = var.tenancy_ocid
  name           = "${var.prefix}-runtime-dg"
  description    = "JetUse Container Instances and Functions resource principals"
  matching_rule  = <<-EOT
    Any {all {resource.type='computecontainerinstance', resource.compartment.id='${var.compartment_ocid}'},
         all {resource.type='fnfunc', resource.compartment.id='${var.compartment_ocid}'}}
  EOT
}

resource "oci_identity_dynamic_group" "adb" {
  compartment_id = var.tenancy_ocid
  name           = "${var.prefix}-adb-dg"
  description    = "JetUse Autonomous Database resource principal"
  matching_rule  = "All {resource.type='autonomousdatabase', resource.compartment.id='${var.compartment_ocid}'}"
}

resource "oci_identity_dynamic_group" "semantic_store" {
  count          = var.enable_semantic_store ? 1 : 0
  compartment_id = var.tenancy_ocid
  name           = "${var.prefix}-semantic-store-dg"
  description    = "JetUse OCI Generative AI semantic store resource principal"
  matching_rule  = "All {resource.type='generativeaisemanticstore', resource.compartment.id='${var.compartment_ocid}'}"
}

locals {
  runtime_statements = [
    # Chat / Responses / Projects / Guardrails / hosted agent invocation.
    "Allow dynamic-group ${oci_identity_dynamic_group.runtime.name} to use generative-ai-family in compartment id ${var.compartment_ocid}",
    # RAG の Vector Store と Files はアプリが作成・削除するため manage が必要。
    "Allow dynamic-group ${oci_identity_dynamic_group.runtime.name} to manage generative-ai-vectorstore in compartment id ${var.compartment_ocid}",
    "Allow dynamic-group ${oci_identity_dynamic_group.runtime.name} to manage generative-ai-vectorstore-file in compartment id ${var.compartment_ocid}",
    "Allow dynamic-group ${oci_identity_dynamic_group.runtime.name} to manage generative-ai-file in compartment id ${var.compartment_ocid}",
    # ADB wallet 取得、RAG/議事録ファイル、AIサービス、可観測性。
    "Allow dynamic-group ${oci_identity_dynamic_group.runtime.name} to use autonomous-database-family in compartment id ${var.compartment_ocid}",
    "Allow dynamic-group ${oci_identity_dynamic_group.runtime.name} to manage objects in compartment id ${var.compartment_ocid}",
    "Allow dynamic-group ${oci_identity_dynamic_group.runtime.name} to read buckets in compartment id ${var.compartment_ocid}",
    "Allow dynamic-group ${oci_identity_dynamic_group.runtime.name} to manage ai-service-speech-family in compartment id ${var.compartment_ocid}",
    "Allow dynamic-group ${oci_identity_dynamic_group.runtime.name} to use ai-service-document-family in compartment id ${var.compartment_ocid}",
    "Allow dynamic-group ${oci_identity_dynamic_group.runtime.name} to use ai-service-language-family in compartment id ${var.compartment_ocid}",
    "Allow dynamic-group ${oci_identity_dynamic_group.runtime.name} to read tag-namespaces in compartment id ${var.compartment_ocid}",
    "Allow dynamic-group ${oci_identity_dynamic_group.runtime.name} to use log-content in compartment id ${var.compartment_ocid}",
    "Allow dynamic-group ${oci_identity_dynamic_group.runtime.name} to use metrics in compartment id ${var.compartment_ocid}",
    # 事前作成された MCP credential secret を読む場合に使用。secret の作成権限は付与しない。
    "Allow dynamic-group ${oci_identity_dynamic_group.runtime.name} to read secret-family in compartment id ${var.compartment_ocid}",
    # API Gateway から Functions ルーターを呼び出す。
    "Allow any-user to use functions-family in compartment id ${var.compartment_ocid} where ALL {request.principal.type = 'ApiGateway', request.resource.compartment.id = '${var.compartment_ocid}'}",
  ]

  adb_statements = [
    # DBMS_CLOUD_AI / Select AI が ADB の resource principal で推論・RAGを行う。
    "Allow dynamic-group ${oci_identity_dynamic_group.adb.name} to use generative-ai-family in compartment id ${var.compartment_ocid}",
    "Allow dynamic-group ${oci_identity_dynamic_group.adb.name} to read objects in compartment id ${var.compartment_ocid}",
  ]

  semantic_store_statements = var.enable_semantic_store ? [
    "Allow dynamic-group ${oci_identity_dynamic_group.semantic_store[0].name} to use database-tools-family in compartment id ${var.compartment_ocid}",
    "Allow dynamic-group ${oci_identity_dynamic_group.semantic_store[0].name} to read secret-family in compartment id ${var.compartment_ocid}",
    "Allow dynamic-group ${oci_identity_dynamic_group.semantic_store[0].name} to read database-family in compartment id ${var.compartment_ocid}",
    "Allow dynamic-group ${oci_identity_dynamic_group.semantic_store[0].name} to read autonomous-database-family in compartment id ${var.compartment_ocid}",
    "Allow dynamic-group ${oci_identity_dynamic_group.semantic_store[0].name} to use generative-ai-family in compartment id ${var.compartment_ocid}",
  ] : []
}

resource "oci_identity_policy" "runtime" {
  compartment_id = var.compartment_ocid
  name           = "${var.prefix}-runtime-policy"
  description    = "JetUse least-privilege runtime permissions"
  statements = concat(
    local.runtime_statements,
    local.adb_statements,
    local.semantic_store_statements,
  )
}

# Object Storage namespace はテナンシ単位のため、コンパートメントポリシーとは分離する。
resource "oci_identity_policy" "runtime_tenancy" {
  compartment_id = var.tenancy_ocid
  name           = "${var.prefix}-runtime-tenancy-policy"
  description    = "JetUse runtime tenancy-level read-only permission"
  statements = [
    "Allow dynamic-group ${oci_identity_dynamic_group.runtime.name} to read objectstorage-namespaces in tenancy",
  ]
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
