output "dynamic_group" {
  value = oci_identity_dynamic_group.app.name
}

output "policy_id" {
  value = oci_identity_policy.app.id
}
