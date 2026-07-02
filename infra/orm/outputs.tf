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

output "runtime_dynamic_group" {
  value = module.iam.runtime_dynamic_group
}

output "adb_dynamic_group" {
  value = module.iam.adb_dynamic_group
}

output "semantic_store_dynamic_group" {
  value = module.iam.semantic_store_dynamic_group
}

output "runtime_policy_id" {
  value = module.iam.runtime_policy_id
}

output "adb_id" {
  value = module.adb.adb_id
}

output "note" {
  value = "初回はIAM反映、ADB作成、DB初期化に10〜15分かかります。app_urlを開き、demo_username/demo_passwordでログインしてください。"
}
