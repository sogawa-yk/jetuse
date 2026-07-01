# Resource Manager が自動入力する。
variable "tenancy_ocid" {
  type = string
}

variable "compartment_ocid" {
  description = "JetUse 専用コンパートメントの OCID"
  type        = string
}

variable "home_region" {
  description = "テナンシのホームリージョン（IAM変更先）"
  type        = string

  validation {
    condition     = trimspace(var.home_region) != ""
    error_message = "home_region はテナンシ詳細に表示されるホームリージョンを指定してください。"
  }
}

variable "prefix" {
  description = "Dynamic Group / Policy のテナンシ内で一意な名前プレフィックス"
  type        = string
  default     = "jetuse"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,19}$", var.prefix))
    error_message = "prefix は英小文字で始まる 2〜20 文字の英小文字・数字・ハイフンにしてください。"
  }
}

variable "create_deployer_policy" {
  description = "既存グループへ JetUse 専用コンパートメントの Resource Manager デプロイ権限を付与する"
  type        = bool
  default     = true
}

variable "deployer_group_subject" {
  description = "OCI Policy の group subject（例: Default/JetUseDeployers）"
  type        = string
  default     = "Default/JetUseDeployers"
}

variable "enable_semantic_store" {
  description = "SQL Search 用 Semantic Store の Dynamic Group / Policy を作成する"
  type        = bool
  default     = true
}
