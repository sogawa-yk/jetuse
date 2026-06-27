# OKE クラスタ＋ノードプール(ADR-0017)。jetuse-dev / ap-osaka-1。
# 専用 VCN/サブネット/NSG は呼び出し側(environments/oke)が新設して OCID を渡す
# (既存 develop VCN は参照しない)。apply・恒常課金は人間ゲート(plan/validate 止まり)。

data "oci_identity_availability_domains" "ads" {
  compartment_id = var.compartment_ocid
}

# node_image_id 未指定時に最新の OKE 対応 Oracle Linux ノードイメージを自動選択するためのソース。
data "oci_containerengine_node_pool_option" "this" {
  node_pool_option_id = "all"
  compartment_id      = var.compartment_ocid
}

locals {
  # node_image_id 自動選択は **クラスタの Kubernetes バージョンに対応** する OKE node image だけに絞る
  # (review-1 対応: 緩い regex で k8s 版と非互換なイメージを掴まない)。OKE の source_name は
  # 例 `Oracle-Linux-8.x-...-OKE-1.30.1-xxx` の形で k8s 版を含むため、`OKE-<ver>` で照合する。
  # `kubernetes_version` の先頭 `v` を除いた版番号(例 1.30.1)を使う。GPU/aarch64 イメージは除外。
  k8s_ver_plain = trimprefix(var.kubernetes_version, "v")
  matched_node_image_ids = [
    for src in data.oci_containerengine_node_pool_option.this.sources :
    src.image_id
    if length(regexall("Oracle-Linux", src.source_name)) > 0
    && length(regexall("GPU", src.source_name)) == 0
    && length(regexall("aarch64", src.source_name)) == 0
    && length(regexall("OKE-${local.k8s_ver_plain}", src.source_name)) > 0
  ]
  auto_node_image_id = try(local.matched_node_image_ids[0], "")

  # 自動選択で k8s 版に一致する image が見つからなければ、明示 node_image_id を必須にする
  # (apply 時に "未解決" を予測可能な失敗にする。空 image_id での apply 失敗を前倒し検出)。
  node_image_id = var.node_image_id != "" ? var.node_image_id : local.auto_node_image_id

  # worker は AD 1 つに最小配置(MVP)。複数 AD 分散は人間承認で拡張。
  node_ad = data.oci_identity_availability_domains.ads.availability_domains[0].name
}

resource "oci_containerengine_cluster" "this" {
  compartment_id     = var.compartment_ocid
  name               = "${var.prefix}-cluster"
  kubernetes_version = var.kubernetes_version
  vcn_id             = var.vcn_id
  type               = var.cluster_type

  endpoint_config {
    # control plane エンドポイントはプライベート既定(public は人間承認時のみ)。
    is_public_ip_enabled = var.is_public_api_endpoint
    subnet_id            = var.k8s_api_subnet_id
    nsg_ids              = var.k8s_api_nsg_ids
  }

  cluster_pod_network_options {
    # VCN-native pod networking(pod が VCN IP を持つ。worker サブネットに収容)。
    cni_type = "OCI_VCN_IP_NATIVE"
  }

  options {
    service_lb_subnet_ids = [var.service_lb_subnet_id]

    kubernetes_network_config {
      # **VCN-native(OCI_VCN_IP_NATIVE)では Pod は pod subnet(=worker subnet)の VCN IP を使う**ため、
      # pods_cidr は指定しない(指定すると node_pool の pod_subnet_ids と二重定義になり apply 後の Pod
      # ネットワーク/worker 登録が矛盾する。review-2 blocker 対応)。pods_cidr は FLANNEL 時のみ意味を持つ。
      services_cidr = var.services_cidr
    }

    # ダッシュボード等のアドオンは無効(最小・攻撃面縮小)。
    add_ons {
      is_kubernetes_dashboard_enabled = false
      is_tiller_enabled               = false
    }
  }

  freeform_tags = {
    "jetuse:managed-by" = "terraform"
    "jetuse:component"  = "oke-l3-platform"
  }
}

resource "oci_containerengine_node_pool" "this" {
  cluster_id         = oci_containerengine_cluster.this.id
  compartment_id     = var.compartment_ocid
  name               = "${var.prefix}-np"
  kubernetes_version = var.kubernetes_version
  node_shape         = var.node_shape

  node_shape_config {
    ocpus         = var.node_ocpus
    memory_in_gbs = var.node_memory_gb
  }

  node_source_details {
    source_type             = "IMAGE"
    image_id                = local.node_image_id
    boot_volume_size_in_gbs = 50
  }

  node_config_details {
    size = var.node_pool_size

    placement_configs {
      availability_domain = local.node_ad
      subnet_id           = var.worker_subnet_id
    }

    # VCN-native の pod は worker サブネット上の VNIC を使う(同一サブネットを指定)。
    node_pool_pod_network_option_details {
      cni_type       = "OCI_VCN_IP_NATIVE"
      pod_subnet_ids = [var.worker_subnet_id]
      pod_nsg_ids    = var.worker_nsg_ids
    }

    nsg_ids = var.worker_nsg_ids
  }

  # ssh_public_key は単一文字列の属性。空なら null でキー無し worker。
  ssh_public_key = var.ssh_public_key != "" ? var.ssh_public_key : null

  freeform_tags = {
    "jetuse:managed-by" = "terraform"
    "jetuse:component"  = "oke-l3-platform"
  }

  lifecycle {
    # 自動選択も明示指定も無い(image_id が空)構成を **plan 時点で hard fail** させる(fail-closed)。
    # k8s 版に対応するノードイメージが無いまま apply すると不可解な失敗になるため前倒しで止める。
    precondition {
      condition     = local.node_image_id != ""
      error_message = "OKE node image を解決できません。kubernetes_version=${var.kubernetes_version} に対応する image が無いか、node_image_id を明示してください。"
    }
  }
}
