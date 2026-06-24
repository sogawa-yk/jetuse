# OIDC client_id(IDCSアプリの name 属性が OAuth client_id)
output "client_id" {
  value = oci_identity_domains_app.spa.name
}

output "demo_username" {
  value = oci_identity_domains_user.demo.user_name
}
