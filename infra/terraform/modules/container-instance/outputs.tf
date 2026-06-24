output "instance_id" {
  value = oci_container_instances_container_instance.this.id
}

output "private_ip" {
  value = data.oci_core_vnic.api.private_ip_address
}
