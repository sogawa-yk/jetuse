# OKE 基盤(ADR-0017)。jetuse-dev 専用 VCN(network.tf)＋ OKE クラスタ/ノードプール(modules/oke)。
# apply・恒常課金・IAM は人間ゲート。本環境は plan/validate まで。

module "oke" {
  source = "../../modules/oke"

  compartment_ocid     = var.compartment_ocid
  prefix               = var.prefix
  vcn_id               = oci_core_vcn.oke.id
  k8s_api_subnet_id    = oci_core_subnet.k8s_api.id
  worker_subnet_id     = oci_core_subnet.worker.id
  service_lb_subnet_id = oci_core_subnet.service_lb.id
  k8s_api_nsg_ids      = [oci_core_network_security_group.k8s_api.id]
  worker_nsg_ids       = [oci_core_network_security_group.worker.id]

  kubernetes_version     = var.kubernetes_version
  is_public_api_endpoint = var.is_public_api_endpoint
  node_pool_size         = var.node_pool_size
  node_ocpus             = var.node_ocpus
  node_memory_gb         = var.node_memory_gb
  node_image_id          = var.node_image_id
  ssh_public_key         = var.ssh_public_key

  # route table / gateway / NSG security rule 群が作成済みになってから cluster/node_pool を作る
  # (review-1 対応。OKE は subnet の routing/NSG が揃っていないと worker 登録が失敗しうる)。
  depends_on = [
    oci_core_route_table.private,
    oci_core_nat_gateway.oke,
    oci_core_service_gateway.oke,
    oci_core_network_security_group_security_rule.api_in_worker_6443,
    oci_core_network_security_group_security_rule.api_in_worker_12250,
    oci_core_network_security_group_security_rule.api_eg_worker,
    oci_core_network_security_group_security_rule.worker_in_worker,
    oci_core_network_security_group_security_rule.worker_in_api,
    oci_core_network_security_group_security_rule.worker_eg_all,
  ]
}
