output "app_url" {
  description = "アプリのURL。ブラウザで開く"
  value       = "https://${module.api_gateway.endpoint}/"
}

output "demo_username" {
  description = "デモログインユーザー(enable_auth=true のとき)"
  value       = var.enable_auth ? module.identity_domain_app[0].demo_username : "（認証無効: ログイン不要）"
}

output "demo_password" {
  description = "デモログインユーザーの初期パスワード"
  # ログインに必要なため RM 出力で表示する。random_password は機微値なので
  # nonsensitive() で明示的にマスクを解除する(プロト用途。本番運用ではVault等を検討)。
  value     = var.enable_auth ? nonsensitive(random_password.demo.result) : ""
  sensitive = false
}

output "oidc_client_id" {
  value = local.oidc_client_id
}

output "identity_domain_url" {
  value = local.domain_url
}

output "adb_id" {
  value = module.adb.adb_id
}

output "note" {
  value = "前提: 管理者が infra/orm-bootstrap を適用済み。初回は ADB 作成とDBブートストラップに10〜15分かかります。app_url を開き、demo_username/demo_password でログインしてください。"
}
