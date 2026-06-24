output "app_log_id" {
  value = oci_logging_log.app.id
}

output "log_group_id" {
  value = oci_logging_log_group.this.id
}
