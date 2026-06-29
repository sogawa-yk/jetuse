# Resource Manager がテンプレート変数として注入(schema.yaml で hidden)。
variable "tenancy_ocid" {
  type = string
}

variable "region" {
  type = string
}

variable "home_region" {
  description = "テナンシのホームリージョン(Identity Domain作成用)"
  type        = string
  default     = "us-ashburn-1"
}

# --- ユーザー入力 ---
variable "compartment_ocid" {
  description = "リソースを作成するコンパートメント"
  type        = string
}

variable "prefix" {
  description = "リソース名プレフィックス"
  type        = string
  default     = "jetuse"
}

variable "adb_admin_password" {
  description = "ADB ADMIN パスワード。空なら自動生成(出力に表示)"
  type        = string
  sensitive   = true
  default     = ""
}

variable "demo_email" {
  description = "デモログインユーザーのメールアドレス"
  type        = string
  default     = "demo@example.com"
}

variable "enable_auth" {
  description = "OIDC認証を有効化(Identity Domain + OIDCアプリ + デモユーザーを作成)"
  type        = bool
  default     = true
}

variable "enable_opensearch" {
  description = "OpenSearch RAGクラスタ(常設課金・高コスト)。既定OFF"
  type        = bool
  default     = false
}

variable "enable_iam" {
  description = "IAM動的グループ+ポリシー(テナンシレベル)を作成。テナンシ管理者でない場合はfalseに"
  type        = bool
  default     = true
}

variable "rate_limit_rps" {
  description = "API Gateway のレート上限(req/秒。0で無効)"
  type        = number
  default     = 20
}

# コンテナイメージは OCIR(ap-osaka-1) に置く(ADR-0011)。Functions は OCIR必須・
# Container Instance も同一OCIRを参照。private のまま Resource Principal で pull。
# 既定は ocir_namespace / ocir_region_key から locals.tf で合成(override 可)。
variable "ocir_region_key" {
  description = "OCIRレジストリのリージョンキー(ap-osaka-1 は kix → kix.ocir.io)"
  type        = string
  default     = "kix"
}

variable "ocir_namespace" {
  description = "OCIRネームスペース(= Object Storage namespace。tenancy固有)"
  type        = string
  default     = "idqcucnenh88"
}

# 明示指定時は合成より優先(空なら ocir_* から合成)。
variable "api_image_url" {
  type    = string
  default = ""
}

# BE-08: 認証付き MCP 登録で資格情報を束ねる既存 Vault と KMS 鍵を「参照」する入力(作成しない)。
# 空なら認証付き登録は 503(fail-closed)。実値は tfvars / ORM 変数で与え、コミットしない。
# 有効化には併せて IAM(use keys / use vaults / manage secret-family。docs/setup/iam.md)が必要(人間ゲート)。
variable "vault_ocid" {
  description = "認証付き MCP の secret を作成する既存 Vault の OCID(参照のみ)"
  type        = string
  default     = ""
}

variable "vault_key_ocid" {
  description = "secret 内容の暗号化に使う既存 KMS 鍵の OCID(参照のみ)"
  type        = string
  default     = ""
}

variable "fn_router_image" {
  type    = string
  default = ""
}
