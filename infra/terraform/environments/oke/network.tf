# OKE 専用 VCN(ADR-0017)。**既存 develop VCN は参照しない**。
# 3 サブネット分割: K8s API エンドポイント / worker+VCN-native pod / service LB。
# 既定は **すべてプライベート**(control plane=private endpoint、worker=private、LB=internal)。
# パブリック化は明示変数で opt-in(`is_public_api_endpoint` / `service_lb_is_public`。人間承認)。
# NSG は OKE 標準(control plane ⇔ worker ⇔ pod ⇔ LB)。apply は人間ゲート(plan/validate 止まり)。

resource "oci_core_vcn" "oke" {
  compartment_id = var.compartment_ocid
  display_name   = "${var.prefix}-vcn"
  cidr_blocks    = [var.vcn_cidr]
  dns_label      = replace(var.prefix, "-", "")
}

# Internet Gateway は **public 経路が要るときだけ** 作る(public API endpoint か public LB の opt-in 時)。
# private 既定では public subnet も IGW も作らない(未使用リソースを残さない)。public RT と同じ
# `local.need_public_rt` で連動させ、IGW を参照する public RT と作成条件を一致させる(review 対応)。
resource "oci_core_internet_gateway" "oke" {
  count          = local.need_public_rt ? 1 : 0
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.oke.id
  display_name   = "${var.prefix}-igw"
}

# NAT Gateway は **VCN レベル** の管理ゲートウェイで、private subnet の egress を担う。
# OCI の NAT GW は public subnet を必要としない(subnet ではなく route table に紐付く)。private 既定でも
# worker/内部 LB が OCIR pull / GenAI / Platform API へ出るために常時必要なので無条件で作成する。
resource "oci_core_nat_gateway" "oke" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.oke.id
  display_name   = "${var.prefix}-natgw"
}

data "oci_core_services" "all" {
  filter {
    name   = "name"
    values = ["All .* Services In Oracle Services Network"]
    regex  = true
  }
}

resource "oci_core_service_gateway" "oke" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.oke.id
  display_name   = "${var.prefix}-sgw"
  services {
    service_id = data.oci_core_services.all.services[0].id
  }
}

# プライベート: NAT(0/0) ＋ Service Gateway(OCIR/GenAI/Object Storage 等)。worker と内部 LB が使う。
resource "oci_core_route_table" "private" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.oke.id
  display_name   = "${var.prefix}-rt-private"
  route_rules {
    destination       = "0.0.0.0/0"
    destination_type  = "CIDR_BLOCK"
    network_entity_id = oci_core_nat_gateway.oke.id
  }
  route_rules {
    destination       = data.oci_core_services.all.services[0].cidr_block
    destination_type  = "SERVICE_CIDR_BLOCK"
    network_entity_id = oci_core_service_gateway.oke.id
  }
}

# パブリック: Internet Gateway。**public opt-in 時のみ作成**(public API endpoint か public LB のとき)。
locals {
  need_public_rt = var.is_public_api_endpoint || var.service_lb_is_public
  # サブネットが参照する route table id を opt-in で切り替える。
  k8s_api_rt_id    = var.is_public_api_endpoint ? oci_core_route_table.public[0].id : oci_core_route_table.private.id
  service_lb_rt_id = var.service_lb_is_public ? oci_core_route_table.public[0].id : oci_core_route_table.private.id
}

resource "oci_core_route_table" "public" {
  count          = local.need_public_rt ? 1 : 0
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.oke.id
  display_name   = "${var.prefix}-rt-public"
  route_rules {
    destination       = "0.0.0.0/0"
    destination_type  = "CIDR_BLOCK"
    network_entity_id = oci_core_internet_gateway.oke[0].id
  }
}

# セキュリティ境界は NSG を主とし、各 subnet の Security List は **default(VCN 既定)を使う**。
# OCI CCM は LoadBalancer Service の listener/health-check 規則を service_lb / worker subnet の
# Security List に自動管理する(OKE 標準動作)ため、ここで空 SL を強制すると初回 LB 作成や CCM の
# seclist 管理と衝突する。SL のさらなる絞り込み(NSG 一本化)は OCI CCM の seclist 管理モードを
# 無効化したうえで実 OKE 検証する follow-up とする(検証レポート §4.5)。
resource "oci_core_subnet" "k8s_api" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.oke.id
  display_name   = "${var.prefix}-k8s-api"
  cidr_block     = var.k8s_api_subnet_cidr
  # public opt-in 時のみ public subnet(IGW route ＋ public IP 許可)。private 既定では NAT route ＋
  # public IP 禁止。route(local.k8s_api_rt_id)と prohibit を **同じ opt-in 変数で連動**させ、IGW route と
  # public IP 許可が必ず一致する(IGW を private subnet に付ける不整合を構造的に防ぐ。review-5/6 対応)。
  route_table_id             = local.k8s_api_rt_id
  dns_label                  = "k8sapi"
  prohibit_public_ip_on_vnic = !var.is_public_api_endpoint
}

resource "oci_core_subnet" "worker" {
  compartment_id             = var.compartment_ocid
  vcn_id                     = oci_core_vcn.oke.id
  display_name               = "${var.prefix}-worker"
  cidr_block                 = var.worker_subnet_cidr
  route_table_id             = oci_core_route_table.private.id
  dns_label                  = "worker"
  prohibit_public_ip_on_vnic = true
}

resource "oci_core_subnet" "service_lb" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.oke.id
  display_name   = "${var.prefix}-svclb"
  cidr_block     = var.service_lb_subnet_cidr
  # public LB opt-in 時のみ public subnet(IGW route ＋ public IP 許可)。internal 既定では NAT route ＋
  # public IP 禁止。route(local.service_lb_rt_id)と prohibit を同じ opt-in 変数で連動させ整合させる。
  route_table_id             = local.service_lb_rt_id
  dns_label                  = "svclb"
  prohibit_public_ip_on_vnic = !var.service_lb_is_public
}

# ---- NSG: K8s API エンドポイント(control plane) ----
resource "oci_core_network_security_group" "k8s_api" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.oke.id
  display_name   = "${var.prefix}-nsg-k8s-api"
}

# worker → API(6443 kube-apiserver / 12250 OKE)
resource "oci_core_network_security_group_security_rule" "api_in_worker_6443" {
  network_security_group_id = oci_core_network_security_group.k8s_api.id
  direction                 = "INGRESS"
  protocol                  = "6"
  source_type               = "NETWORK_SECURITY_GROUP"
  source                    = oci_core_network_security_group.worker.id
  tcp_options {
    destination_port_range {
      min = 6443
      max = 6443
    }
  }
}

resource "oci_core_network_security_group_security_rule" "api_in_worker_12250" {
  network_security_group_id = oci_core_network_security_group.k8s_api.id
  direction                 = "INGRESS"
  protocol                  = "6"
  source_type               = "NETWORK_SECURITY_GROUP"
  source                    = oci_core_network_security_group.worker.id
  tcp_options {
    destination_port_range {
      min = 12250
      max = 12250
    }
  }
}

# 管理元(operator/CI/bastion)→ API(6443)。worker 以外から kube-apiserver に到達できないと
# apply 後の運用(kubectl)が詰まるため、承認済み CIDR を最小範囲で許可する(review-5 対応)。
# 既定(空)は VCN 内のみ(bastion 経由)。public endpoint でも 0.0.0.0/0 は使わない。
locals {
  admin_api_cidrs = length(var.admin_api_allowed_cidrs) > 0 ? var.admin_api_allowed_cidrs : [var.vcn_cidr]
}

resource "oci_core_network_security_group_security_rule" "api_in_admin_6443" {
  for_each                  = toset(local.admin_api_cidrs)
  network_security_group_id = oci_core_network_security_group.k8s_api.id
  direction                 = "INGRESS"
  protocol                  = "6"
  source_type               = "CIDR_BLOCK"
  source                    = each.value
  tcp_options {
    destination_port_range {
      min = 6443
      max = 6443
    }
  }
}

# Path MTU discovery(ICMP type 3 code 4)を worker から許可。
resource "oci_core_network_security_group_security_rule" "api_in_worker_icmp" {
  network_security_group_id = oci_core_network_security_group.k8s_api.id
  direction                 = "INGRESS"
  protocol                  = "1"
  source_type               = "NETWORK_SECURITY_GROUP"
  source                    = oci_core_network_security_group.worker.id
  icmp_options {
    type = 3
    code = 4
  }
}

# control plane → worker(kubelet 10250 等)＋ OCI services(SGW)。
resource "oci_core_network_security_group_security_rule" "api_eg_worker" {
  network_security_group_id = oci_core_network_security_group.k8s_api.id
  direction                 = "EGRESS"
  protocol                  = "6"
  destination_type          = "NETWORK_SECURITY_GROUP"
  destination               = oci_core_network_security_group.worker.id
}

resource "oci_core_network_security_group_security_rule" "api_eg_services" {
  network_security_group_id = oci_core_network_security_group.k8s_api.id
  direction                 = "EGRESS"
  protocol                  = "6"
  destination_type          = "SERVICE_CIDR_BLOCK"
  destination               = data.oci_core_services.all.services[0].cidr_block
}

# ---- NSG: worker ノード ----
resource "oci_core_network_security_group" "worker" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.oke.id
  display_name   = "${var.prefix}-nsg-worker"
}

# worker 間(pod 間通信含む)は全許可。
resource "oci_core_network_security_group_security_rule" "worker_in_worker" {
  network_security_group_id = oci_core_network_security_group.worker.id
  direction                 = "INGRESS"
  protocol                  = "all"
  source_type               = "NETWORK_SECURITY_GROUP"
  source                    = oci_core_network_security_group.worker.id
}

# control plane → worker(kubelet/exec/logs)。
resource "oci_core_network_security_group_security_rule" "worker_in_api" {
  network_security_group_id = oci_core_network_security_group.worker.id
  direction                 = "INGRESS"
  protocol                  = "6"
  source_type               = "NETWORK_SECURITY_GROUP"
  source                    = oci_core_network_security_group.k8s_api.id
}

# LB → worker(NodePort/health check)。OCI LB の VNIC は **service LB サブネットに作られる**ため、
# NSG 関連付けに依存せず確実に許可できるよう **サブネット CIDR** を源にする(review-1 blocker 対応)。
# OCI CCM に NSG を関連付ける場合は Service の
# `oci.oraclecloud.com/oci-network-security-groups` annotation を併用する(下の svclb NSG)。
resource "oci_core_network_security_group_security_rule" "worker_in_lb" {
  network_security_group_id = oci_core_network_security_group.worker.id
  direction                 = "INGRESS"
  protocol                  = "6"
  source_type               = "CIDR_BLOCK"
  source                    = var.service_lb_subnet_cidr
  tcp_options {
    destination_port_range {
      # OKE の NodePort レンジ。LB はこのレンジへ転送・ヘルスチェックする。
      min = 30000
      max = 32767
    }
  }
}

# worker → 全 egress(OCIR pull / GenAI / Platform API / NAT 経由)。
resource "oci_core_network_security_group_security_rule" "worker_eg_all" {
  network_security_group_id = oci_core_network_security_group.worker.id
  direction                 = "EGRESS"
  protocol                  = "all"
  destination_type          = "CIDR_BLOCK"
  destination               = "0.0.0.0/0"
}

# ---- NSG: service LB(CCM が annotation で関連付ける任意の NSG) ----
resource "oci_core_network_security_group" "service_lb" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.oke.id
  display_name   = "${var.prefix}-nsg-svclb"
}

# LB へのインバウンド。既定の本体 Service は **平文 HTTP(80)**(uvicorn:8000 を素通し。TLS 終端は
# 上流の API Gateway/Ingress)。LB で TLS 終端する運用のときだけ 443 を使うため、80/443 の両方を
# 許可する(service.yaml の port と NSG を整合させる。review F-002)。public LB のときのみ 0.0.0.0/0、
# internal のときは VCN 内に閉じる。
resource "oci_core_network_security_group_security_rule" "lb_in_app" {
  for_each                  = toset(["80", "443"])
  network_security_group_id = oci_core_network_security_group.service_lb.id
  direction                 = "INGRESS"
  protocol                  = "6"
  source_type               = "CIDR_BLOCK"
  source                    = var.service_lb_is_public ? "0.0.0.0/0" : var.vcn_cidr
  tcp_options {
    destination_port_range {
      min = tonumber(each.value)
      max = tonumber(each.value)
    }
  }
}

# LB → worker(NodePort レンジ)。
resource "oci_core_network_security_group_security_rule" "lb_eg_worker" {
  network_security_group_id = oci_core_network_security_group.service_lb.id
  direction                 = "EGRESS"
  protocol                  = "6"
  destination_type          = "CIDR_BLOCK"
  destination               = var.worker_subnet_cidr
  tcp_options {
    destination_port_range {
      min = 30000
      max = 32767
    }
  }
}
