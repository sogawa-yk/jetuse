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
resource "oci_objectstorage_preauthrequest" "spa_read" {
  namespace    = local.ns
  bucket       = oci_objectstorage_bucket.spa.name
  name         = "${var.prefix}-spa-read"
  access_type  = "AnyObjectRead"
  time_expires = var.spa_par_expiry
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
