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

  # VCN dns_label は replace(prefix,"-","") で導出され OCI 上限は15文字(超過で apply が
  # LimitExceeded/InvalidParameter で落ちる — docs/tips.md 2026-07-13)。英小文字始まり。
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]*$", var.prefix)) && length(replace(var.prefix, "-", "")) <= 15
    error_message = "prefix は英小文字で始まり、英小文字/数字/ハイフンのみ。ハイフンを除いた長さは15文字以内(VCN dns_label 上限)にしてください。"
  }
}

variable "allow_unvalidated_genai_region" {
  description = "GenAI(推論+agentic API)が検証済みでないリージョンへのデプロイを明示的に許可する。JetUseのRAG/会話メモリ/デモ生成はGenAIに依存するため、未検証リージョンでは動作未保証(実証済=大阪kix/シカゴord)。"
  type        = bool
  default     = false
}

variable "ci_shape" {
  description = "API コンテナ(Container Instance)の shape。E4.Flex 非提供リージョン向けに可変(既定は現行値)"
  type        = string
  default     = "CI.Standard.E4.Flex"
}

# ADB は既定でロック(db_version=26ai / ECPU=2)。新規テナンシは ADB ECPU 枠が 0 のことがあり
# apply が LimitExceeded で落ちる。26ai 非提供のリージョン/レルムでも落ちる。事前に枠と提供状況を確認。
variable "adb_db_version" {
  description = "ADB の db_version。Select AI ベクトル索引は 26ai(旧23ai系) 必須。提供状況をリージョンで確認"
  type        = string
  default     = "26ai"
}

variable "adb_ecpu_count" {
  description = "ADB の ECPU 数。新規テナンシは ECPU サービス枠が 0 のことがあるため事前確認(既定 2)"
  type        = number
  default     = 2
}

# NL2SQL(SQL Search)は事前作成された Generative AI Semantic Store の OCID を要する。
# enable_semantic_store の IAM(DG+policy)はこれを使う権限を与えるだけで実体は作らない。
# 空なら NL2SQL は 503(未設定)。使う場合はストアを作成し OCID をここに設定する。
variable "semstore_ocid" {
  description = "NL2SQL(SQL Search)が使う Generative AI Semantic Store の OCID(任意。空なら NL2SQL 無効)"
  type        = string
  default     = ""
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

# コンテナイメージは JetUse 公開レジストリ(対応4リージョン kix/nrt/iad/ord の OCIR)へ事前 push
# 済み(ADR-0011/0017)。既定値はその公開 namespace で、cross-tenancy pull に必須なので通常は変更しない。
# 自テナンシへイメージをミラーした場合のみ上書きする(その場合は api_image_url/fn_router_image も併せて指定)。
# Functions は同一リージョンの OCIR 必須のため、レジストリはデプロイリージョンから自動導出(locals.tf)。
variable "ocir_namespace" {
  description = "JetUse 公開イメージのレジストリ namespace。通常は変更しない(自テナンシの Object Storage namespace とは無関係)。変更時は api_image_url と fn_router_image の両方明示が必須(region_guard で強制)"
  type        = string
  # 既定値は local.public_ocir_namespace と一致させること(region_guard の precondition が参照)。
  default = "idqcucnenh88"
}

# イメージrepo名のプレフィックス。リソース名の var.prefix とは分離する(設計上の独立)。
# release.yml が push する repo 名は固定(jetuse-api / jetuse-fn-router)なので、
# prefix を変えてもイメージ参照が壊れないよう、ここは既定 "jetuse" を使う。
variable "image_repo_prefix" {
  description = "OCIRイメージrepo名のプレフィックス(release.ymlのpush先と一致させる。既定 jetuse)"
  type        = string
  default     = "jetuse"
}

# FIX-47: DP 状態API(Files / Vector Store files / Conversations)は OpenAi-Project ヘッダに
# GenerativeAiProject OCID が必須。空ならアプリが compartment 内の ACTIVE project を自動検出し、
# 無ければ自動作成する(GenerativeAiProject は TF provider 未対応のためアプリ側で解決)。
variable "project_ocid" {
  description = "GenerativeAI Project OCID(空なら自動検出/自動作成)"
  type        = string
  default     = ""
}

# 自動作成の opt-in(既定 on — ワンクリックの無手動セットアップを成立させる)。
# off にする場合は project_ocid の明示指定が実質必須。IAM の
# 'manage generative-ai-project' もこのフラグと連動する。
variable "enable_project_autocreate" {
  description = "GenerativeAI Project の自動作成を許可(IAM policy 'manage generative-ai-project' を含む)"
  type        = bool
  default     = true
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
