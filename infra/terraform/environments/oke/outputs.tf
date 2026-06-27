output "cluster_id" {
  value = module.oke.cluster_id
}

output "cluster_name" {
  value = module.oke.cluster_name
}

# K8s API エンドポイント(環境依存の実値)。リポジトリ方針で sensitive。
output "kubernetes_api_endpoint" {
  value     = module.oke.kubernetes_api_endpoint
  sensitive = true
}

output "node_pool_id" {
  value = module.oke.node_pool_id
}

output "vcn_id" {
  value     = oci_core_vcn.oke.id
  sensitive = true
}

# service LB 用 NSG の OCID。apply 後に本体/デモ Service へ
# `oci.oraclecloud.com/oci-network-security-groups=<this>` annotation を付与して LB↔worker を NSG で
# 固めるための実値(README §1・SKIPPED.md の手順)。プレースホルダをコミットしないための output。
output "service_lb_nsg_id" {
  value     = oci_core_network_security_group.service_lb.id
  sensitive = true
}
