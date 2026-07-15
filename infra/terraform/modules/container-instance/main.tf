data "oci_identity_availability_domains" "ads" {
  compartment_id = var.compartment_ocid
}

resource "oci_container_instances_container_instance" "this" {
  compartment_id      = var.compartment_ocid
  display_name        = "${var.prefix}-api"
  availability_domain = data.oci_identity_availability_domains.ads.availability_domains[0].name
  shape               = var.shape

  shape_config {
    ocpus         = var.ocpus
    memory_in_gbs = var.memory_gb
  }

  vnics {
    subnet_id             = var.subnet_id
    is_public_ip_assigned = false
    nsg_ids               = [var.nsg_id]
  }

  containers {
    display_name          = "api"
    image_url             = var.image_url
    environment_variables = var.environment_variables
  }

  dynamic "image_pull_secrets" {
    for_each = var.image_pull_secret_id == "" ? [] : [1]
    content {
      registry_endpoint = split("/", var.image_url)[0]
      secret_type       = "VAULT"
      secret_id         = var.image_pull_secret_id
    }
  }

  dynamic "image_pull_secrets" {
    for_each = var.registry_username == "" ? [] : [1]
    content {
      registry_endpoint = split("/", var.image_url)[0]
      secret_type       = "BASIC"
      username          = base64encode(var.registry_username)
      password          = base64encode(var.registry_password)
    }
  }
}

data "oci_core_vnic" "api" {
  vnic_id = oci_container_instances_container_instance.this.vnics[0].vnic_id
}
