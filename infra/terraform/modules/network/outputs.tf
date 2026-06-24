output "vcn_id" {
  value = oci_core_vcn.this.id
}

output "vcn_cidr" {
  value = var.vcn_cidr
}

output "public_subnet_id" {
  value = oci_core_subnet.public.id
}

output "private_subnet_id" {
  value = oci_core_subnet.private.id
}

output "apigw_nsg_id" {
  value = oci_core_network_security_group.apigw.id
}

output "app_nsg_id" {
  value = oci_core_network_security_group.app.id
}
