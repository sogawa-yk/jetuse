variable "compartment_ocid" {
  description = "jetuse-dev コンパートメント OCID(実値は .env / TF_VAR。コミットしない)"
  type        = string
}

variable "prefix" {
  description = "リソース名の接頭辞(例: jetuse-dev-oke)"
  type        = string
}

variable "vcn_id" {
  description = "OKE を載せる専用 VCN の OCID(environments/oke が新設)"
  type        = string
}

variable "k8s_api_subnet_id" {
  description = "Kubernetes API エンドポイント(control plane)用サブネット OCID(プライベート)"
  type        = string
}

variable "worker_subnet_id" {
  description = "worker ノード用サブネット OCID(プライベート)"
  type        = string
}

variable "service_lb_subnet_id" {
  description = "type=LoadBalancer Service 用サブネット OCID"
  type        = string
}

variable "k8s_api_nsg_ids" {
  description = "Kubernetes API エンドポイントに付ける NSG OCID 群"
  type        = list(string)
  default     = []
}

variable "worker_nsg_ids" {
  description = "worker ノードに付ける NSG OCID 群"
  type        = list(string)
  default     = []
}

variable "kubernetes_version" {
  description = "OKE Kubernetes バージョン(例: v1.30.1)。apply 時に実在版へ合わせる"
  type        = string
  default     = "v1.30.1"
}

variable "cluster_type" {
  description = "OKE クラスタ種別(BASIC_CLUSTER | ENHANCED_CLUSTER)"
  type        = string
  default     = "ENHANCED_CLUSTER"
}

variable "is_public_api_endpoint" {
  description = "K8s API エンドポイントをパブリック公開するか(既定 false=プライベート。public は人間承認時のみ)"
  type        = bool
  default     = false
}

# 注: VCN-native(OCI_VCN_IP_NATIVE)では Pod は pod subnet(=worker subnet)の VCN IP を使うため
# pods_cidr は設定しない(FLANNEL 専用パラメータ)。worker サブネットの広さで Pod 数を収容する。

variable "services_cidr" {
  description = "Kubernetes Service の ClusterIP CIDR(クラスタ内部)"
  type        = string
  default     = "10.96.0.0/16"
}

variable "node_pool_size" {
  description = "ノードプールの worker 数(最小構成。恒常課金。apply は人間ゲート)"
  type        = number
  default     = 2
}

variable "node_shape" {
  description = "worker ノードの shape"
  type        = string
  default     = "VM.Standard.E4.Flex"
}

variable "node_ocpus" {
  description = "Flex shape の OCPU 数/ノード"
  type        = number
  default     = 2
}

variable "node_memory_gb" {
  description = "Flex shape のメモリ(GB)/ノード"
  type        = number
  default     = 16
}

variable "node_image_id" {
  description = "worker ノードイメージ OCID。空なら最新の OKE 対応 OL イメージを自動選択"
  type        = string
  default     = ""
}

variable "ssh_public_key" {
  description = "worker ノードへの SSH 公開鍵(任意。空ならキー無し)"
  type        = string
  default     = ""
}
