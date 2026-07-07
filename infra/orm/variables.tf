# Resource Manager がテンプレート変数として注入(schema.yaml で hidden)。
variable "tenancy_ocid" {
  type = string
}

variable "region" {
  type = string
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

# SP2-04 fail-closed: AUTH_REQUIRED=true の API は issuer/audience/JWKS の3点必須(不備は全リクエスト500)。
# enable_auth=true で不備のまま配備しない強制は main.tf の terraform_data.oidc_config_guard
# (precondition。変数間 validation は TF>=1.9 のため required_version 1.5 では使えない — review-2 M001)。
variable "oidc_audience" {
  description = "APIが検証するOIDC audience。Identity Domainアプリ登録のprimary audience、または実トークンのaud実測値(discoveryからは得られない — specs/18 §5.1)。enable_auth=true では必須"
  type        = string
  default     = ""
}

variable "oidc_issuer" {
  description = "トークンのiss。世代で異なるため固定値を持たない(specs/18 §5.1 — discoveryのissuerをそのまま入力)。enable_auth=true では必須"
  type        = string
  default     = ""
}

variable "oidc_jwks_url" {
  description = "discoveryのjwks_uri。空なら本スタックが作成するドメインの <domain_url>/admin/v1/SigningCert/jwk を使う(実測でdiscoveryと同一パス)"
  type        = string
  default     = ""
}

variable "enable_dynamic_group" {
  description = "Runtime / ADB / Semantic StoreのDynamic Groupとテナンシスコープのnamespace参照ポリシーを作成する"
  type        = bool
  default     = true
}

# enable_dynamic_group=false のとき、runtime policyの全statementが参照する既存Dynamic Group名。
variable "existing_dynamic_group" {
  description = "既存のDynamic Group名(enable_dynamic_group=falseの場合必須。Container Instance / Functions / ADB / Semantic Storeを含むmatching ruleであること)"
  type        = string
  default     = ""
}

variable "enable_runtime_policy" {
  description = "JetUse専用コンパートメントにランタイムポリシーを作成する"
  type        = bool
  default     = true
}

variable "enable_semantic_store" {
  description = "SQL Search用Semantic StoreのDynamic Group / Policyを有効にする"
  type        = bool
  default     = true
}

variable "enable_opensearch" {
  description = "OpenSearch RAGクラスタ(常設課金・高コスト)。既定OFF"
  type        = bool
  default     = false
}

variable "rate_limit_rps" {
  description = "API Gateway のレート上限(req/秒。0で無効)"
  type        = number
  default     = 20
}

# コンテナイメージは対応4リージョン(kix/nrt/iad/ord)の OCIR へ事前 push(ADR-0011/0017)。
# Functions は同一リージョンの OCIR 必須のため、レジストリはデプロイリージョンから自動導出
# (locals.tf)。リージョンキーはユーザー入力にしない。
variable "ocir_namespace" {
  description = "OCIRネームスペース(= Object Storage namespace。tenancy固有)"
  type        = string
  default     = "idqcucnenh88"
}

# イメージrepo名のプレフィックス。リソース名の var.prefix とは分離する(設計上の独立)。
# release.yml が push する repo 名は固定(jetuse-api / jetuse-fn-router)なので、
# prefix を変えてもイメージ参照が壊れないよう、ここは既定 "jetuse" を使う。
variable "image_repo_prefix" {
  description = "OCIRイメージrepo名のプレフィックス(release.ymlのpush先と一致させる。既定 jetuse)"
  type        = string
  default     = "jetuse"
}

# 明示指定時は合成より優先(空なら ocir_* / image_repo_prefix から合成)。
variable "api_image_url" {
  type    = string
  default = ""
}

variable "fn_router_image" {
  type    = string
  default = ""
}
