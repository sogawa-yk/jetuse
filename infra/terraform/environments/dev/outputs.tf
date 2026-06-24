output "apigw_hostname" {
  value = module.api_gateway.endpoint
}

output "os_namespace" {
  value = module.object_storage.namespace
}

output "spa_bucket" {
  value = module.object_storage.spa_bucket
}

output "functions_application_id" {
  value = module.functions.application_id
}

output "adb_id" {
  value = length(module.adb) == 0 ? null : module.adb[0].adb_id
}

output "identity_domain_url" {
  value = length(module.identity_domain) == 0 ? null : module.identity_domain[0].domain_url
}

output "api_private_ip" {
  value = length(module.container_instance) == 0 ? null : module.container_instance[0].private_ip
}

output "opensearch_endpoint" {
  value = length(module.opensearch) == 0 ? null : module.opensearch[0].endpoint
}

# --- 開発者ごとアプリ・スタック(environments/app)が remote_state で参照する共有値 ---
# いずれも既存リソースの参照のみ。リソースは変更しない(追加出力だけ)。

output "compartment_ocid" {
  value = var.compartment_ocid
}

output "vcn_id" {
  value = module.network.vcn_id
}

output "vcn_cidr" {
  value = module.network.vcn_cidr
}

output "public_subnet_id" {
  value = module.network.public_subnet_id
}

output "private_subnet_id" {
  value = module.network.private_subnet_id
}

output "apigw_nsg_id" {
  value = module.network.apigw_nsg_id
}

output "app_nsg_id" {
  value = module.network.app_nsg_id
}

output "app_data_bucket" {
  value = module.object_storage.app_data_bucket
}

output "speech_bucket" {
  value = module.object_storage.speech_bucket
}

output "app_log_id" {
  value = module.observability.app_log_id
}
