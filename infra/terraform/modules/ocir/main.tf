resource "oci_artifacts_container_repository" "this" {
  for_each = toset(var.repositories)

  compartment_id = var.compartment_ocid
  display_name   = "${var.prefix}-${each.value}"
  is_public      = false
}
