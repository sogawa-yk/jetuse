output "adb_id" {
  value = oci_database_autonomous_database.this.id
}

output "db_name" {
  value = oci_database_autonomous_database.this.db_name
}

output "connection_strings" {
  value     = oci_database_autonomous_database.this.connection_strings
  sensitive = true
}

# base64エンコードされたウォレットzip(wallet_password 指定時のみ。INFRA-03)
output "wallet_content_b64" {
  value     = length(oci_database_autonomous_database_wallet.this) == 0 ? "" : oci_database_autonomous_database_wallet.this[0].content
  sensitive = true
}
