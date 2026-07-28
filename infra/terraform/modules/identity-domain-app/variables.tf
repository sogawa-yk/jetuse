variable "prefix" {
  type = string
}

variable "idcs_endpoint" {
  description = "Identity DomainのIDCSエンドポイント(https://idcs-xxxx.identity.oraclecloud.com)"
  type        = string
}

variable "redirect_uri" {
  description = "OIDCリダイレクトURI(= https://<API GWホスト>/)"
  type        = string
}

variable "demo_email" {
  description = "デモログインユーザーのメール"
  type        = string
  default     = "demo@example.com"
}

variable "demo_password" {
  description = "デモユーザーの初期パスワード(自動生成)"
  type        = string
  sensitive   = true
}

# Identity Domain はホームリージョンにあるため、oci CLI の呼び出しにも同リージョンを渡す。
variable "home_region" {
  description = "テナンシのホームリージョン(Identity Domain の所在。空ならCLI既定に委ねる)"
  type        = string
  default     = ""
}
