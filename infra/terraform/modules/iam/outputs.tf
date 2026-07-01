output "runtime_dynamic_group" {
  value = oci_identity_dynamic_group.runtime.name
}

output "adb_dynamic_group" {
  value = oci_identity_dynamic_group.adb.name
}

output "semantic_store_dynamic_group" {
  value = var.enable_semantic_store ? oci_identity_dynamic_group.semantic_store[0].name : null
}

output "runtime_policy_id" {
  value = oci_identity_policy.runtime.id
}

output "runtime_tenancy_policy_id" {
  value = oci_identity_policy.runtime_tenancy.id
}

output "deployer_policy_id" {
  value = var.create_deployer_policy ? oci_identity_policy.deployer[0].id : null
}

# 旧呼び出し元との互換出力。新規コードでは上の責務別 output を使う。
output "dynamic_group" {
  value = oci_identity_dynamic_group.runtime.name
}

output "policy_id" {
  value = oci_identity_policy.runtime.id
}
