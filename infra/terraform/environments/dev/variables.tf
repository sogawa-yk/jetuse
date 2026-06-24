variable "region" {
  type    = string
  default = "ap-osaka-1"
}

variable "home_region" {
  description = "テナンシのホームリージョン(Identity系CREATE用)"
  type        = string
  default     = "us-ashburn-1"
}

variable "tenancy_ocid" {
  type = string
}

variable "compartment_ocid" {
  type = string
}

variable "prefix" {
  description = "リソース名プレフィックス。エージェント単独のapply検証時は jetuse-spike-tf を使うこと"
  type        = string
  default     = "jetuse-dev"
}

variable "vcn_cidr" {
  description = "既存VCN develop(10.0.0.0/16)と重複しないこと"
  type        = string
  default     = "10.1.0.0/16"
}

variable "adb_admin_password" {
  type      = string
  sensitive = true
  default   = ""
}

variable "enable_adb" {
  type    = bool
  default = true
}

# ENH-05: OpenSearch RAGクラスタ(常設課金)。既定OFF。有効化はユーザー承認のうえ。
variable "enable_opensearch" {
  type    = bool
  default = false
}

variable "api_image_url" {
  description = "FastAPIコンテナイメージ。空ならContainer Instanceを作らない(初回applyはOCIRが空のため)"
  type        = string
  default     = ""
}

variable "image_pull_secret_id" {
  type    = string
  default = ""
}

variable "registry_username" {
  description = "OCIR BASIC認証({namespace}/{user})。privateリポジトリのpullに必要"
  type        = string
  default     = ""
}

variable "registry_password" {
  description = "OCIR authトークン(.envのOCIR_TOKEN)"
  type        = string
  default     = ""
  sensitive   = true
}

variable "functions_routes" {
  description = "API GWのFunctionsルート(パスセグメント→fn OCID)。APP-01でfnデプロイ後に追加"
  type        = map(string)
  default     = {}
}

variable "api_environment" {
  description = "Container Instanceに渡す環境変数(AUTH_REQUIRED, OIDC_*等)"
  type        = map(string)
  default     = {}
  sensitive   = true
}

variable "enable_identity_domain" {
  description = "JetUseアプリ用の専用Identity Domain(INFRA-02、ユーザー承認2026-06-10)"
  type        = bool
  default     = true
}

variable "enable_iam" {
  description = "動的グループ+ポリシー。エージェント用ユーザーにはテナンシ権限がなく404になるため既定false(2026-06-10実測)。人間が docs/setup/app-iam.md で実施"
  type        = bool
  default     = false
}

variable "fn_router_image" {
  description = "fnルーターのOCIRイメージURL(ARCH-02。空なら未デプロイ)"
  type        = string
  default     = ""
}

variable "rate_limit_rps" {
  description = "SEC-03: GW全体のレート上限(req/秒。0で無効)"
  type        = number
  default     = 0
}
