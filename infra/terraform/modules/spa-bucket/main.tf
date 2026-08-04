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
# 相対期限の基準時刻を state に固定(object-storage モジュールと同挙動。ignore_changes 不要)。
resource "time_offset" "spa_par" {
  count        = var.spa_par_expiry == "" ? 1 : 0
  offset_years = 1
}

resource "oci_objectstorage_preauthrequest" "spa_read" {
  namespace   = local.ns
  bucket      = oci_objectstorage_bucket.spa.name
  name        = "${var.prefix}-spa-read"
  access_type = "AnyObjectRead"
  # 空なら apply 時刻(time_offset の base)起点 +1年。明示指定時はその値を尊重(変更も反映)。
  time_expires = var.spa_par_expiry != "" ? var.spa_par_expiry : time_offset.spa_par[0].rfc3339
}
