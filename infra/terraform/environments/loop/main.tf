# ループエンジニアリングの実環境 E2E 用 ADB（jetuse-dev / 固定 loop 環境）。
# 「むやみに増やさない・再利用する」方針（CLAUDE.md / memory jetuse-dev-terraform-resources-ok）。
# 作り直す場合は terraform destroy → apply。
# 環境依存値（compartment OCID・パスワード）はコミットしない → TF_VAR_ で渡す。
terraform {
  required_version = ">= 1.5"
  required_providers {
    oci = {
      source = "oracle/oci"
    }
  }
}

provider "oci" {
  region = var.region
  # 認証は ~/.oci/config の DEFAULT プロファイル（API キー）。
}

variable "region" {
  type    = string
  default = "ap-osaka-1"
}

variable "compartment_ocid" {
  description = "jetuse-dev コンパートメント OCID（TF_VAR_compartment_ocid で渡す。リポジトリにコミットしない）"
  type        = string
}

variable "admin_password" {
  description = "ADB ADMIN パスワード（TF_VAR_admin_password で渡す）"
  type        = string
  sensitive   = true
}

module "adb" {
  source           = "../../modules/adb"
  compartment_ocid = var.compartment_ocid
  prefix           = "jetuse-loop"
  # 26ai（モジュール既定）。最小構成 2 ECPU / 20GB。ウォレットは db.py が OCID から API 生成するため不要。
  generate_wallet = false
  admin_password  = var.admin_password
}

output "adb_id" {
  value = module.adb.adb_id
}

output "db_name" {
  value = module.adb.db_name
}
