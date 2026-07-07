# SPA(コミット済み dist)を spa バケットへ配置し、config.json を実行時設定として上書きする。
# infra/orm/spa.tf と同一方針。env は environments/dev-app にあるため dist は repo ルート(4階層上)。
locals {
  spa_dist_dir = "${path.module}/../../../../packages/web/dist"
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

resource "oci_objectstorage_object" "spa" {
  for_each = local.spa_files

  namespace     = module.spa.namespace
  bucket        = module.spa.spa_bucket
  object        = each.value
  source        = "${local.spa_dist_dir}/${each.value}"
  content_type  = lookup(local.mime, element(reverse(split(".", each.value)), 0), "application/octet-stream")
  cache_control = startswith(each.value, "assets/") ? "public, max-age=31536000, immutable" : "no-cache"
}

# プレビューは認証オフ。SPA は /config.json を実行時に読む。
resource "oci_objectstorage_object" "config_json" {
  namespace = module.spa.namespace
  bucket    = module.spa.spa_bucket
  object    = "config.json"
  content = jsonencode({
    authRequired  = false
    oidcAuthority = ""
    oidcClientId  = ""
  })
  content_type  = "application/json; charset=utf-8"
  cache_control = "no-cache"
}
