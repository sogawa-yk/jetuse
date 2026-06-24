output "cluster_id" {
  value = oci_opensearch_opensearch_cluster.this.id
}

output "opensearch_fqdn" {
  value = oci_opensearch_opensearch_cluster.this.opensearch_fqdn
}

output "opensearch_private_ip" {
  value = oci_opensearch_opensearch_cluster.this.opensearch_private_ip
}

# REST APIエンドポイント。security_mode=DISABLED でも 9200 はTLS(HTTPS)。
# 作成中は private_ip が null になるため null セーフにする。
output "endpoint" {
  value = try("https://${oci_opensearch_opensearch_cluster.this.opensearch_private_ip}:9200", null)
}
