terraform {
  required_version = ">= 1.5"
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

provider "oci" {
  alias  = "home"
  region = [for r in data.oci_identity_region_subscriptions.this.region_subscriptions : r.region_name if r.is_home_region][0]
}
