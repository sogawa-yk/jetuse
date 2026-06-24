output "repository_names" {
  value = { for k, r in oci_artifacts_container_repository.this : k => r.display_name }
}
