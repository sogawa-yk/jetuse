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
  # ドメインの deactivate は IAM 操作なのでテナンシのホームリージョンでしか実行できない
  # (既定の大阪へ投げると 403 "go to your home region")。ホームリージョンを動的取得し
  # --region で明示する。再destroy安全のため ACTIVE のときだけ実行。
  # destroy-time provisioner は self のみ参照可。
  provisioner "local-exec" {
    when    = destroy
    command = <<-CMD
      HOME_REGION=$(oci iam region-subscription list --query 'data[?"is-home-region"]|[0]."region-name"' --raw-output)
      STATE=$(oci iam domain get --domain-id ${self.id} --region "$HOME_REGION" --query 'data."lifecycle-state"' --raw-output 2>/dev/null || echo GONE)
      if [ "$STATE" = "ACTIVE" ]; then
        oci iam domain deactivate --domain-id ${self.id} --region "$HOME_REGION" --wait-for-state SUCCEEDED --max-wait-seconds 600
      fi
    CMD
  }
}
