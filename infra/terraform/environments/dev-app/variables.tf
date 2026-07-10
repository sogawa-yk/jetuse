variable "compartment_ocid" {
  type = string
}

variable "prefix" {
  type    = string
  default = "jetuse-dev-app"
}

variable "region" {
  type    = string
  default = "ap-osaka-1"
}

variable "vcn_cidr" {
  type    = string
  default = "10.9.0.0/16"
}

variable "public_subnet_cidr" {
  type    = string
  default = "10.9.1.0/24"
}

variable "private_subnet_cidr" {
  type    = string
  default = "10.9.2.0/24"
}

variable "app_port" {
  type    = number
  default = 8000
}

variable "image_url" {
  type = string
}

variable "registry_username" {
  type = string
}

variable "registry_password" {
  type      = string
  sensitive = true
}

variable "ocpus" {
  type    = number
  default = 1
}

variable "memory_gb" {
  type    = number
  default = 4
}

# --- アプリ環境変数（フラット変数。RM ネイティブ）。 ---
# region / compartment は上位変数から導出するため env には持たせない。
# 秘匿値（ADB パスワード類）は sensitive。本番へは値だけ差し替えて promote する。
variable "auth_mode" {
  type    = string
  default = "resource_principal"
}

variable "project_ocid" {
  description = "GenAI プロジェクト OCID"
  type        = string
}

variable "os_namespace" {
  type    = string
  default = "idqcucnenh88"
}

variable "adb_ocid" {
  description = "再利用 ADB の OCID（wallet はリソースプリンシパルで実行時生成）"
  type        = string
}

variable "adb_dsn" {
  type    = string
  default = "jetuseloop2_low"
}

variable "adb_user" {
  type    = string
  default = "JETUSE_APP"
}

variable "adb_password" {
  type      = string
  sensitive = true
}

variable "adb_query_user" {
  type    = string
  default = "JETUSE_APP_Q"
}

variable "adb_query_password" {
  type      = string
  sensitive = true
}

variable "adb_wallet_password" {
  type      = string
  sensitive = true
}

# 生成 SPA バンドル(demo-bundles/ prefix)と RAG 文書の保管バケット。未設定 = 生成デモの
# /app/ 配信と RAG が 404/無効(review-2 B001 — loop 環境と同じバケット名を RM 変数で与える)
variable "rag_bucket" {
  type    = string
  default = ""
}

# 生成 SPA の app-session(一回性コード/Cookie)の HMAC 秘密鍵。未設定 = 発行 500(fail-closed)
variable "app_session_secret" {
  type      = string
  sensitive = true
  default   = ""
}

# --- SP3-06/07: フロント生成(sign_proxy は API プロセス内 mount — /gen-proxy)。 ---
# 既定 localhost = 同一プロセス mount の自己参照。SP3-08(生成 CI)は runtime が自 IP へ解決する。
variable "generation_proxy_url" {
  type    = string
  default = "http://localhost:8000/gen-proxy/v1"
}

# ORASEJAPAN 共有テナンシ(gpt-5 系)の compartment OCID(非鍵材料。ops/deploy-dev-app.sh
# seed-env でシード)。鍵材料そのものは RM 変数に置かない — Vault シークレット(vault.tf)へ
# seed-env が投入する(SP3-09)。空 = 共有モデル fail-closed(403)。
variable "gen_shared_compartment_ocid" {
  type    = string
  default = ""
}
