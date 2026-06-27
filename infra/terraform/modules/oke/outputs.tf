output "cluster_id" {
  value = oci_containerengine_cluster.this.id
}

output "cluster_name" {
  value = oci_containerengine_cluster.this.name
}

# K8s API エンドポイント(環境依存の実値)。リポジトリ方針に従い sensitive(ログ流出予防)。
output "kubernetes_api_endpoint" {
  value     = oci_containerengine_cluster.this.endpoints
  sensitive = true
}

output "node_pool_id" {
  value = oci_containerengine_node_pool.this.id
}

output "resolved_node_image_id" {
  value = local.node_image_id
}
