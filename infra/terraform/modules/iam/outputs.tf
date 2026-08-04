output "runtime_dynamic_group" {
  value = try(oci_identity_dynamic_group.runtime[0].name, null)
}

output "adb_dynamic_group" {
  value = try(oci_identity_dynamic_group.adb[0].name, null)
}

output "semantic_store_dynamic_group" {
  value = try(oci_identity_dynamic_group.semantic_store[0].name, null)
}

output "runtime_policy_id" {
  value = try(oci_identity_policy.runtime[0].id, null)
}

output "runtime_tenancy_policy_id" {
  value = try(oci_identity_policy.runtime_tenancy[0].id, null)
}

output "deployer_policy_id" {
  value = var.create_deployer_policy ? oci_identity_policy.deployer[0].id : null
}

# 旧呼び出し元との互換出力。新規コードでは上の責務別 output を使う。
output "dynamic_group" {
  value = try(oci_identity_dynamic_group.runtime[0].name, null)
}

output "policy_id" {
  value = try(oci_identity_policy.runtime[0].id, null)
}

# PORT-03: IAM の**内容**が変わったかを呼び出し元が検出するための指紋。
# DG 名や policy OCID は matching rule 本文や statement 差し替えでは変わらないため、
# 反映待ち(time_sleep)の trigger にはこちらを使う。
output "content_fingerprint" {
  value = sha256(jsonencode({
    runtime_matching_rule = local.runtime_matching_rule
    runtime_statements    = try(oci_identity_policy.runtime[0].statements, [])
    adb_matching_rule     = try(oci_identity_dynamic_group.adb[0].matching_rule, "")
  }))
}
