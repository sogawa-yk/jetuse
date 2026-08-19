variable "region" {
  type = string
  # AGT-06: 既定はシカゴ(GenAI のモデル品揃えが厚い)。大阪も値を渡せば使える。
  default = "us-chicago-1"
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

variable "enable_dynamic_group" {
  description = "Runtime用動的グループをテナンシ直下に作成。エージェント用ユーザーにはテナンシ権限がないため既定false"
  type        = bool
  default     = false
}

variable "enable_runtime_policy" {
  description = "既存動的グループを参照するランタイムポリシーを対象コンパートメントに作成。既存ポリシーとの競合を避けるため既定false"
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

variable "spa_par_expiry" {
  description = <<-D
    SPA 配信 PAR の失効日時(RFC3339)。空なら apply 時刻起点 +1年の相対期限。

    **state を作り直すときは既存値を明示する。** 空のままだと time_offset が新しい基準時刻で
    作られ、PAR が **replace = URL が変わる**（API Gateway の配線もやり直しになる）。
    ORM への移行(ADR-0031)のように state を持ち替える場面では、現行 PAR の time_expires を
    ここに入れて据え置く。
  D
  type        = string
  default     = ""
}

variable "existing_dynamic_group" {
  description = <<-D
    `enable_dynamic_group = false` のとき、ランタイムポリシーが参照する**既存の動的グループ名**。

    テナンシの DynamicResourceGroups には上限があり、環境ごとに 3 本ずつ作ると足りない
    (2026-08-08 実測: 50 本で quotaExceeded。うち 48 本は他プロジェクトのもので消せない)。
    共用する場合、その DG の matching rule が対象コンパートメントの Container Instance /
    Functions / ADB を含んでいる必要がある。**含めないとポリシーだけができて権限は付かない。**
  D
  type        = string
  default     = ""
}
