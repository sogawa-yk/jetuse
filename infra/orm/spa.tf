# INFRA-03: アプリ層の成果物を Terraform で配置する。
#  1) ADBウォレット(base64テキスト) → app-data バケット(コンテナがobject readで取得・デコード)
#  2) SPA(コミット済み dist) → spa バケット(API GW が配信)
#  3) config.json(OIDC client_id 込み) → spa バケット(SPAが実行時に取得)

# base64テキストのため content にそのまま渡せる(バイナリzipのUTF-8問題を回避)
resource "oci_objectstorage_object" "adb_wallet" {
  namespace    = module.object_storage.namespace
  bucket       = module.object_storage.app_data_bucket
  object       = "adb_wallet.zip.b64"
  content      = module.adb.wallet_content_b64
  content_type = "text/plain"
}

# SPA dist のパス(release.yml が main で生成・コミット)
locals {
  spa_dist_dir = "${path.module}/../../packages/web/dist"
  mime = {
    html  = "text/html; charset=utf-8"
    js    = "text/javascript; charset=utf-8"
    mjs   = "text/javascript; charset=utf-8"
    css   = "text/css; charset=utf-8"
    json  = "application/json; charset=utf-8"
    svg   = "image/svg+xml"
    png   = "image/png"
    webp  = "image/webp"
    jpg   = "image/jpeg"
    jpeg  = "image/jpeg"
    ico   = "image/x-icon"
    woff2 = "font/woff2"
    map   = "application/json"
    txt   = "text/plain; charset=utf-8"
  }
  # config.json は Terraform 生成版で上書きするため一括アップロードから除外
  spa_files = toset([
    for f in fileset(local.spa_dist_dir, "**") : f if f != "config.json"
  ])
}

# --- 1) SPA 静的ファイル ---
resource "oci_objectstorage_object" "spa" {
  for_each = local.spa_files

  namespace    = module.object_storage.namespace
  bucket       = module.object_storage.spa_bucket
  object       = each.value
  source       = "${local.spa_dist_dir}/${each.value}"
  content_type = lookup(local.mime, element(reverse(split(".", each.value)), 0), "application/octet-stream")
  # ハッシュ付きアセットは長期キャッシュ、それ以外は都度検証
  cache_control = startswith(each.value, "assets/") ? "public, max-age=31536000, immutable" : "no-cache"
}

# --- 2) 実行時設定(OIDC client_id 込み)。OIDCアプリ作成後に書く ---
resource "oci_objectstorage_object" "config_json" {
  namespace = module.object_storage.namespace
  bucket    = module.object_storage.spa_bucket
  object    = "config.json"
  content = jsonencode({
    authRequired  = var.enable_auth
    oidcAuthority = local.domain_url
    oidcClientId  = local.oidc_client_id
  })
  content_type  = "application/json; charset=utf-8"
  cache_control = "no-cache"
}
