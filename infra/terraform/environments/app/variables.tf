variable "dev_name" {
  description = "開発者識別子(英小文字)。リソースは jetuse-<dev_name>-* で作られる。例: alice"
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9]{1,12}$", var.dev_name))
    error_message = "dev_name は英小文字+数字、先頭は英字、2〜13文字。"
  }
}

variable "region" {
  type    = string
  default = "ap-osaka-1"
}

variable "shared_state_path" {
  description = "共有基盤(environments/dev)のローカルstateへのパス"
  type        = string
  default     = "../dev/terraform.tfstate"
}

variable "api_image_url" {
  description = "OCIRイメージ。不変shaタグ推奨。例: kix.ocir.io/<ns>/jetuse-dev-api:dev-alice-9f3c1a2"
  type        = string
}

variable "registry_username" {
  description = "OCIR BASIC認証ユーザー({namespace}/{user})"
  type        = string
  default     = ""
}

variable "registry_password" {
  description = "OCIR authトークン"
  type        = string
  default     = ""
  sensitive   = true
}

variable "image_pull_secret_id" {
  type    = string
  default = ""
}

variable "api_environment" {
  description = "CIへ注入する環境変数。共有値(REGION/COMPARTMENT/PROJECT/SEMSTORE/ADB_DSN/ADB_WALLET_*など)と本人スキーマのADB_PASSWORD/ADB_QUERY_PASSWORD等を含める。AUTH_REQUIRED/ADB_USER/ADB_QUERY_USER/LOG_OCIDはmainで上書きされる"
  type        = map(string)
  default     = {}
  sensitive   = true
}

variable "auth_required" {
  description = "dev環境は既定false(OIDCリダイレクトURI登録の手間を回避し、分離は専用スキーマで担保)"
  type        = bool
  default     = false
}

variable "adb_user" {
  description = "本人のアプリスキーマ。例: JETUSE_ALICE。ops/setup-dev-schema.pyで事前作成"
  type        = string
}

variable "adb_query_user" {
  description = "本人の読取専用ユーザー。例: JETUSE_ALICE_Q"
  type        = string
}

variable "apigw_allow_cidr" {
  description = "API Gateway 443 を許可する送信元CIDR。auth_required=false の公開dev環境は社内/VPNのIPに絞ることを推奨。全公開は 0.0.0.0/0"
  type        = string
  default     = "0.0.0.0/0"
}

variable "rate_limit_rps" {
  description = "GW全体のレート上限(req/秒、0で無効)"
  type        = number
  default     = 0
}

variable "memory_gb" {
  type    = number
  default = 4
}
