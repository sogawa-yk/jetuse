resource "oci_artifacts_container_repository" "this" {
  for_each = toset(var.repositories)

  compartment_id = var.compartment_ocid
  display_name   = "${var.prefix}-${each.value}"
  # public にすると Container Instance / Functions が認証なしで pull 可能(ADR-0011)。
  is_public = var.is_public
}
