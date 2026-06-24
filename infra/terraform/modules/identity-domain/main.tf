# JetUseアプリのエンドユーザー認証用の専用Identity Domain(specs/06)。
# テナンシ管理者(Default domain)からアプリ利用者を分離する。
resource "oci_identity_domain" "this" {
  compartment_id = var.compartment_ocid
  display_name   = "${var.prefix}-domain"
  description    = "JetUse app end-user authentication (OIDC PKCE)"
  home_region    = var.region
  license_type   = "free"
}
