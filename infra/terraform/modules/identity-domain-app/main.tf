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
  # OAuthは認証のみに使うため、初回ログイン時のスコープ同意画面を出さない
  bypass_consent = true

  # destroy前に非アクティブ化(activeなアプリは削除できず destroy が400で失敗するため)。
  # destroy-time provisioner は self のみ参照可。oci CLI は RM 実行環境/ローカルとも利用可能。
  provisioner "local-exec" {
    when    = destroy
    command = <<-CMD
      # `app patch` に --force は無い(あるのは user-password-changer 等)。CLI は複合型の
      # 置換で y/N を尋ねるため、非対話環境では y を流し込む(2026-07-28 実機で
      # "No such option: --force" により destroy が失敗するのを確認)。
      echo y | oci identity-domains app patch \
        --endpoint "${self.idcs_endpoint}" \
        --app-id ${self.id} \
        --schemas '["urn:ietf:params:scim:api:messages:2.0:PatchOp"]' \
        --operations '[{"op": "replace", "path": "active", "value": false}]'
    CMD
  }
}

# デモログインユーザー(アクティベーションメールを待たずログイン可能)。
# パスワードはここでは**設定しない**。SCIM の User リソースへ管理者がパスワードを書くと
# Identity Domains が passwordState.mustChange=true を必ず立て、初回サインインが
# /ui/v1/pwdmustchange へ飛んで出力の demo_password ではログインできなくなるため
# (mustChange は readOnly で PATCH しても無視される — 2026-07-28 実機確定)。
# 代わりに下の UserPasswordChanger で設定する(これは mustChange=false になる)。
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

  active = true
}

# パスワード設定(UserPasswordChanger)。Terraform provider にリソースが無いため OCI CLI を使う。
# Resource Manager の実行環境には oci CLI が委任トークンで認証済みの状態で同梱されている
# (2026-07-28 実機確認)。ローカル terraform 実行時は oci CLI の設定が必要。
# パスワードを変えたら再実行されるよう triggers_replace に入れる(値は state に出ない)。
resource "terraform_data" "demo_password" {
  triggers_replace = [oci_identity_domains_user.demo.id, sha256(var.demo_password)]

  provisioner "local-exec" {
    environment = {
      JETUSE_DEMO_PASSWORD = var.demo_password
    }
    command = <<-CMD
      # 位置パラメータで渡す($@)。REGION_ARG="--region" "xxx" のような代入は
      # 「xxx をコマンドとして実行」になり、フラグが CLI へ届かない。
      set -- ${var.home_region == "" ? "" : "--region \"${var.home_region}\""}
      # パスワード履歴違反(pwdpolicyViolation)は**推測で成功扱いにしない**。
      # 「mustChange=false」も「直前の試行が別の理由で失敗した」も、要求した値が現在の
      # パスワードである証明にはならず、成功扱いにすると出力の demo_password では
      # ログインできない状態を隠してしまう。keepers により毎回新しい値を発行しているので、
      # 通常この分岐には入らない。入った場合は復旧手順を出して失敗させる。
      attempt=0
      while [ "$attempt" -lt 3 ]; do
        attempt=$((attempt + 1))
        out="$(oci identity-domains user-password-changer put --force \
             --endpoint "${var.idcs_endpoint}" "$@" \
             --user-password-changer-id "${oci_identity_domains_user.demo.id}" \
             --schemas '["urn:ietf:params:scim:schemas:oracle:idcs:UserPasswordChanger"]' \
             --password "$JETUSE_DEMO_PASSWORD" 2>&1)" && exit 0
        if printf '%s' "$out" | grep -q 'pwdpolicyViolation'; then
          echo "デモユーザーのパスワードがパスワード履歴と衝突しました。要求した値が現在のパスワードである保証が無いため成功扱いにしません。復旧するには random_password.demo を置き換えて(例: terraform apply -replace=random_password.demo / Resource Manager では keepers の値を進める)新しいパスワードで再実行してください" >&2
          exit 1
        fi
        printf 'user-password-changer failed (attempt %s): %s\n' "$attempt" "$out" >&2
        sleep 10
      done
      echo "デモユーザーのパスワード設定に失敗しました。oci CLI の認証と Identity Domain の権限を確認してください" >&2
      exit 1
    CMD
  }
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
