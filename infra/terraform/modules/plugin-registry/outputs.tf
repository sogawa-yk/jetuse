output "namespace" {
  description = "Object Storage namespace(テナンシ識別子のため sensitive)"
  value       = local.ns
  sensitive   = true
}

output "bucket_name" {
  description = "レジストリバケット名(REGISTRY_BUCKET に設定する)"
  value       = oci_objectstorage_bucket.registry.name
}

output "registry_read_par_uri" {
  description = "読取配布用 PAR の access_uri(相対パス `/p/.../n/.../b/.../o/`)。enable_read_par=false なら null"
  value       = var.enable_read_par ? oci_objectstorage_preauthrequest.registry_read[0].access_uri : null
  sensitive   = true
}

output "registry_read_base_url" {
  description = "取込ベースURL(絶対URL)。末尾は PAR の `/o/`。object 名を先頭スラッシュ無しで直接連結する(例 `<base>index.json` / `<base>plugins/...`)。各 JetUse インスタンスが OCI 資格情報なしに取得する入口。enable_read_par=false なら null"
  value = var.enable_read_par ? format(
    "https://objectstorage.%s.oraclecloud.com%s",
    var.region,
    oci_objectstorage_preauthrequest.registry_read[0].access_uri,
  ) : null
  sensitive = true
}
