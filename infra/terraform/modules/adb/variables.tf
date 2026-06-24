variable "compartment_ocid" {
  type = string
}

variable "prefix" {
  type = string
}

variable "admin_password" {
  type      = string
  sensitive = true
}

variable "db_workload" {
  description = "OLTP(ATP) or DW(ADW)。アプリデータ用途はOLTP"
  type        = string
  default     = "OLTP"
}

variable "ecpu_count" {
  type    = number
  default = 2
}

variable "storage_gb" {
  type    = number
  default = 20
}

variable "db_version" {
  description = "ADBバージョン。Select AIベクトル索引は23ai必須(SPIKE-08)"
  type        = string
  default     = "26ai"
}

variable "generate_wallet" {
  description = "mTLSウォレットを生成して base64 出力する(INFRA-03 ORM)。"
  type        = bool
  default     = false
}

variable "wallet_password" {
  description = "mTLSウォレット生成用パスワード(generate_wallet=true時に使用)"
  type        = string
  sensitive   = true
  default     = ""
}
