# JetUseアプリのエンドユーザー認証用の専用Identity Domain(specs/06)。
# テナンシ管理者(Default domain)からアプリ利用者を分離する。
resource "oci_identity_domain" "this" {
  compartment_id = var.compartment_ocid
  display_name   = "${var.prefix}-domain"
  description    = "JetUse app end-user authentication (OIDC PKCE)"
  home_region    = var.region
  license_type   = "free"

  # destroy前に非アクティブ化(ACTIVEなドメインは削除できず destroy が失敗するため)。
  # 依存順序により、配下のOIDCアプリ削除(非アクティブ化込み)の後にここが走る。
  # destroy-time provisioner は self のみ参照可。
  provisioner "local-exec" {
    when    = destroy
    command = "oci iam domain deactivate --domain-id ${self.id} --wait-for-state SUCCEEDED --max-wait-seconds 600"
  }
}
