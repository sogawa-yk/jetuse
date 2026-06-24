# 開発者ごとのSPA配信バケット + 読取PAR(ADR-0004方式A)。
# object-storage モジュールの spa 部分だけを切り出したもの。app-data/speech は共有のまま。
data "oci_objectstorage_namespace" "this" {
  compartment_id = var.compartment_ocid
}

locals {
  ns = data.oci_objectstorage_namespace.this.namespace
}

resource "oci_objectstorage_bucket" "spa" {
  compartment_id = var.compartment_ocid
  namespace      = local.ns
  name           = "${var.prefix}-spa"
  access_type    = "NoPublicAccess"
}

# bucket_listing_actionは未指定=リスト不可。"Deny"明示はAPIが値を返さず
# 毎applyで再作成(URL変化)になるため指定しない(object-storageモジュールと同挙動)
resource "oci_objectstorage_preauthrequest" "spa_read" {
  namespace    = local.ns
  bucket       = oci_objectstorage_bucket.spa.name
  name         = "${var.prefix}-spa-read"
  access_type  = "AnyObjectRead"
  time_expires = var.spa_par_expiry
}
