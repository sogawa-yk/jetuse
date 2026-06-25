# plugin-registry モジュールの plan 検証用ルート(PLG-04)。
# 目的は `terraform init && terraform validate && terraform plan` を通すこと(**apply はしない**)。
# 環境依存値(compartment OCID)はコミットせず TF_VAR_compartment_ocid で渡す。
#   cd infra/terraform/modules/plugin-registry/examples/plan-check
#   terraform init -backend=false
#   TF_VAR_compartment_ocid="$JETUSE_DEV_COMPARTMENT_OCID" terraform plan
# 認証は ~/.oci/config の DEFAULT プロファイル(API キー)。

terraform {
  required_version = ">= 1.5"
  required_providers {
    oci = {
      source  = "oracle/oci"
      version = ">= 6.0"
    }
    time = {
      source  = "hashicorp/time"
      version = ">= 0.9"
    }
  }
}

provider "oci" {
  region = var.region
}

variable "region" {
  type    = string
  default = "ap-osaka-1"
}

variable "compartment_ocid" {
  description = "jetuse-dev コンパートメント OCID(TF_VAR_ で渡す。コミットしない)"
  type        = string
  sensitive   = true
}

module "plugin_registry" {
  source           = "../.."
  compartment_ocid = var.compartment_ocid
  prefix           = "jetuse-registry"
  region           = var.region
}

output "bucket_name" {
  value = module.plugin_registry.bucket_name
}

output "namespace" {
  value     = module.plugin_registry.namespace
  sensitive = true
}
