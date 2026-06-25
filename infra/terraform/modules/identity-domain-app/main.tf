# INFRA-03(ORMワンクリック): 作成済みIdentity Domainに OIDC(PKCE/public)アプリと
# デモログインユーザーを自動登録する。client_id を出力し、SPAの config.json へ載せる。
terraform {
  required_providers {
    oci = {
      source  = "oracle/oci"
      version = ">= 6.0"
    }
  }
}

# 署名証明書(JWKS)をAPI側が匿名取得できるよう公開する。
# 既定はfalseで /admin/v1/SigningCert/jwk が401になり、APIのJWT検証が失敗するため必須(INFRA-03実機確定)。
resource "oci_identity_domains_setting" "this" {
  idcs_endpoint              = var.idcs_endpoint
  setting_id                 = "Settings"
  schemas                    = ["urn:ietf:params:scim:schemas:oracle:idcs:Settings"]
  signing_cert_public_access = true
  csr_access                 = "none"
}

# SPA用 OIDC パブリッククライアント(Authorization Code + PKCE)
resource "oci_identity_domains_app" "spa" {
  idcs_endpoint = var.idcs_endpoint
  schemas       = ["urn:ietf:params:scim:schemas:oracle:idcs:App"]
  display_name  = "${var.prefix}-spa"

  based_on_template {
    value = "CustomWebAppTemplateId"
  }

  is_oauth_client           = true
  client_type               = "public" # PKCE(公開クライアント)
  allowed_grants            = ["authorization_code"]
  redirect_uris             = [var.redirect_uri]
  post_logout_redirect_uris = [var.redirect_uri]
  is_login_target           = true
  show_in_my_apps           = true
  active                    = true

  # destroy前に非アクティブ化(activeなアプリは削除できず destroy が400で失敗するため)。
  # destroy-time provisioner は self のみ参照可。oci CLI は RM 実行環境/ローカルとも利用可能。
  provisioner "local-exec" {
    when    = destroy
    command = <<-CMD
      oci identity-domains app patch \
        --endpoint "${self.idcs_endpoint}" \
        --app-id ${self.id} \
        --schemas '["urn:ietf:params:scim:api:messages:2.0:PatchOp"]' \
        --operations '[{"op": "replace", "path": "active", "value": false}]'
    CMD
  }
}

# デモログインユーザー(パスワード直接設定。アクティベーションメールを待たずログイン可能)
resource "oci_identity_domains_user" "demo" {
  idcs_endpoint = var.idcs_endpoint
  schemas       = ["urn:ietf:params:scim:schemas:core:2.0:User"]
  user_name     = "demo"

  name {
    family_name = "User"
    given_name  = "Demo"
  }

  emails {
    value   = var.demo_email
    type    = "work"
    primary = true
  }
  emails {
    value   = var.demo_email
    type    = "recovery"
    primary = false
  }

  password = var.demo_password
  active   = true
}

# デモユーザーをSPAアプリへ割当
resource "oci_identity_domains_grant" "demo" {
  idcs_endpoint   = var.idcs_endpoint
  schemas         = ["urn:ietf:params:scim:schemas:oracle:idcs:Grant"]
  grant_mechanism = "ADMINISTRATOR_TO_USER"

  app {
    value = oci_identity_domains_app.spa.id
  }
  grantee {
    value = oci_identity_domains_user.demo.id
    type  = "User"
  }
}
