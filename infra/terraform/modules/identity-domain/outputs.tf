output "domain_id" {
  value = oci_identity_domain.this.id
}

# IDCSエンドポイント(https://idcs-xxxx.identity.oraclecloud.com)。OIDC issuerの基底
output "domain_url" {
  value = oci_identity_domain.this.url
}
