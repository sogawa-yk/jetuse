output "namespace" {
  value = local.ns
}

output "spa_bucket" {
  value = oci_objectstorage_bucket.spa.name
}

output "app_data_bucket" {
  value = oci_objectstorage_bucket.app_data.name
}

output "speech_bucket" {
  value = oci_objectstorage_bucket.speech.name
}

# 例: /p/<token>/n/<ns>/b/<bucket>/o/ — API GWバックエンドURLの基底に使う
output "spa_par_access_uri" {
  value     = oci_objectstorage_preauthrequest.spa_read.access_uri
  sensitive = true
}
