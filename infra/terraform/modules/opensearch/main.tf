# OCI Search with OpenSearch クラスタ(ENH-05)。最小構成・常設課金(SPIKE-E2参照)。
# プライベートサブネット配置。CI(app NSG・egress all)から 9200 で到達するため、
# 専用NSGで 9200/9300 を VCN内から許可する。security_mode=DISABLED(プライベート前提)。

resource "oci_core_network_security_group" "opensearch" {
  compartment_id = var.compartment_ocid
  vcn_id         = var.vcn_id
  display_name   = "${var.prefix}-nsg-opensearch"
}

# REST(9200) と ノード間(9300) を VCN内から許可
resource "oci_core_network_security_group_security_rule" "os_in_9200" {
  network_security_group_id = oci_core_network_security_group.opensearch.id
  direction                 = "INGRESS"
  protocol                  = "6"
  source_type               = "CIDR_BLOCK"
  source                    = var.vcn_cidr
  tcp_options {
    destination_port_range {
      min = 9200
      max = 9200
    }
  }
}

resource "oci_core_network_security_group_security_rule" "os_in_9300" {
  network_security_group_id = oci_core_network_security_group.opensearch.id
  direction                 = "INGRESS"
  protocol                  = "6"
  source_type               = "CIDR_BLOCK"
  source                    = var.vcn_cidr
  tcp_options {
    destination_port_range {
      min = 9300
      max = 9300
    }
  }
}

resource "oci_core_network_security_group_security_rule" "os_egress" {
  network_security_group_id = oci_core_network_security_group.opensearch.id
  direction                 = "EGRESS"
  protocol                  = "all"
  destination_type          = "CIDR_BLOCK"
  destination               = "0.0.0.0/0"
}

resource "oci_opensearch_opensearch_cluster" "this" {
  compartment_id        = var.compartment_ocid
  display_name          = "${var.prefix}-opensearch"
  software_version      = var.software_version
  vcn_id                = var.vcn_id
  subnet_id             = var.subnet_id
  vcn_compartment_id    = var.compartment_ocid
  subnet_compartment_id = var.compartment_ocid
  nsg_id                = oci_core_network_security_group.opensearch.id

  security_mode = "DISABLED"

  master_node_count           = 1
  master_node_host_type       = "FLEX"
  master_node_host_ocpu_count = var.master_ocpu
  master_node_host_memory_gb  = var.master_memory_gb

  data_node_count           = 1
  data_node_host_type       = "FLEX"
  data_node_host_ocpu_count = var.data_ocpu
  data_node_host_memory_gb  = var.data_memory_gb
  data_node_storage_gb      = var.data_storage_gb

  opendashboard_node_count           = 1
  opendashboard_node_host_ocpu_count = var.dashboard_ocpu
  opendashboard_node_host_memory_gb  = var.dashboard_memory_gb
}
