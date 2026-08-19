terraform {
  required_version = ">= 1.5.0"
  required_providers {
    oci = {
      source  = "oracle/oci"
      version = ">= 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.5"
    }
    time = {
      source  = "hashicorp/time"
      version = ">= 0.9"
    }
  }
}

# Resource Manager がプリンシパル認証を注入する。region は schema.yaml の hidden 変数(${region})。
provider "oci" {
  region = var.region
}

# Identity系のCREATEはホームリージョン必須。ユーザー入力は誤入力で失敗するため
# region subscriptionsから自動導出する(deployer policyの inspect tenancies で参照可)。
data "oci_identity_region_subscriptions" "this" {
  tenancy_id = var.tenancy_ocid
}

# 権限不足だと region_subscriptions は **null** になり(401/404 ではない)、生の for 式は
# "Iteration over null value" で落ちる。原因が権限だと分からないメッセージになるので、
# ここは locals.home_region(try 付き)を使い、判定と案内は main.tf の region_guard に集約する。
provider "oci" {
  alias  = "home"
  region = local.home_region
}
