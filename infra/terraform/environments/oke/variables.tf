variable "region" {
  description = "OCI リージョン(ADR-0017: ap-osaka-1 固定)"
  type        = string
  default     = "ap-osaka-1"
}

variable "compartment_ocid" {
  description = "jetuse-dev コンパートメント OCID(実値は TF_VAR_compartment_ocid / .env。コミットしない)"
  type        = string
}

variable "prefix" {
  description = "リソース名の接頭辞(VCN dns_label にも使うため DNS ラベル制約内に収める)"
  type        = string
  default     = "jetuse-dev-oke"
  validation {
    # `replace(prefix,"-","")` を VCN dns_label に使う。dns_label は英字始まり・英数字のみ・<=15。
    # 数字始まり/長すぎる prefix が apply 時に遅延失敗しないよう、入口で fail-closed 検証する。
    condition = (
      can(regex("^[a-z][a-z0-9-]*$", var.prefix)) &&
      length(replace(var.prefix, "-", "")) > 0 &&
      length(replace(var.prefix, "-", "")) <= 15
    )
    error_message = "prefix は英小文字始まり・[a-z0-9-] のみ・ハイフン除去後 1〜15 文字にすること(VCN dns_label 制約)。"
  }
}

variable "vcn_cidr" {
  description = "OKE 専用 VCN の CIDR(既存 develop VCN とは別空間)"
  type        = string
  default     = "10.10.0.0/16"
}

variable "k8s_api_subnet_cidr" {
  description = "K8s API エンドポイント(control plane)サブネット CIDR"
  type        = string
  default     = "10.10.0.0/28"
}

variable "worker_subnet_cidr" {
  description = "worker / VCN-native pod サブネット CIDR(pod 数を収容できる広さ)"
  type        = string
  default     = "10.10.16.0/20"
}

variable "service_lb_subnet_cidr" {
  description = "type=LoadBalancer Service 用サブネット CIDR"
  type        = string
  default     = "10.10.32.0/24"
}

variable "kubernetes_version" {
  description = "OKE Kubernetes バージョン(apply 時に実在版へ合わせる)"
  type        = string
  default     = "v1.30.1"
}

variable "node_pool_size" {
  description = "worker ノード数(最小構成。恒常課金。apply は人間ゲート)"
  type        = number
  default     = 2
}

variable "node_ocpus" {
  type    = number
  default = 2
}

variable "node_memory_gb" {
  type    = number
  default = 16
}

variable "node_image_id" {
  description = "worker ノードイメージ OCID(空なら自動選択)"
  type        = string
  default     = ""
}

variable "ssh_public_key" {
  description = "worker への SSH 公開鍵(任意)"
  type        = string
  default     = ""
}

variable "is_public_api_endpoint" {
  description = "K8s API をパブリック公開するか(既定 false=プライベート)"
  type        = bool
  default     = false
}

variable "service_lb_is_public" {
  description = "type=LoadBalancer Service を外部公開(public LB)にするか(既定 false=内部 LB。外部公開は人間承認 opt-in)"
  type        = bool
  default     = false
}

variable "admin_api_allowed_cidrs" {
  description = <<-EOT
    Kubernetes API エンドポイント(6443)へ kubectl 到達を許す管理元 CIDR 群
    (operator / CI / bastion)。worker からの 6443/12250 は別途常に許可。
    既定は VCN 内のみ(bastion 経由運用を想定)。public endpoint でも 0.0.0.0/0 は使わず承認済み CIDR に限定する。
  EOT
  type        = list(string)
  default     = []
  validation {
    # CIDR 形式(`a.b.c.d/n` 等)であること。`cidrhost` が解釈できない値は拒否する。
    condition     = alltrue([for c in var.admin_api_allowed_cidrs : can(cidrhost(trimspace(c), 0))])
    error_message = "admin_api_allowed_cidrs は CIDR 表記(例 10.10.0.0/16)で指定すること。"
  }
  validation {
    # **prefix 長 0(= /0)を構造的に拒否**: 0.0.0.0/0・::/0 だけでなく任意アドレスの /0 全開放を
    # 表記ゆれに依らず弾く(文字列完全一致のすり抜け対策。tfvars ミスでの kube-apiserver 全公開防止)。
    condition = alltrue([
      for c in var.admin_api_allowed_cidrs :
      can(cidrhost(trimspace(c), 0)) ? tonumber(split("/", trimspace(c))[1]) > 0 : true
    ])
    error_message = "admin_api_allowed_cidrs に prefix 長 0(/0=全開放)は指定できません(限定 CIDR を使う)。"
  }
}
