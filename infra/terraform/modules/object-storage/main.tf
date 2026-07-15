data "oci_objectstorage_namespace" "this" {
  compartment_id = var.compartment_ocid
}

locals {
  ns = data.oci_objectstorage_namespace.this.namespace
}

# SPAビルド成果物（非公開。配信はAPI GW経由 + バケット読取PAR — ADR-0004）
resource "oci_objectstorage_bucket" "spa" {
  compartment_id = var.compartment_ocid
  namespace      = local.ns
  name           = "${var.prefix}-spa"
  access_type    = "NoPublicAccess"
}

# bucket_listing_actionは未指定=リスト不可。"Deny"を明示するとAPIが値を
# 返さず毎applyで再作成(URL変化)になるため指定しない
# 相対期限の基準時刻を state に固定する(time_offset)。timestamp() と違い base を保持するため
# plan 毎に揺れず、ignore_changes 無しで安定する(=明示指定時の後からの変更も反映できる)。
# 既存(固定日付)スタックでは初回 apply で PAR が 1回だけ相対期限へ再発行される(URL は API GW へ再配線)。
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

resource "oci_objectstorage_bucket" "app_data" {
  compartment_id = var.compartment_ocid
  namespace      = local.ns
  name           = "${var.prefix}-app-data"
  access_type    = "NoPublicAccess"
}

resource "oci_objectstorage_bucket" "speech" {
  compartment_id = var.compartment_ocid
  namespace      = local.ns
  name           = "${var.prefix}-speech"
  access_type    = "NoPublicAccess"
}
