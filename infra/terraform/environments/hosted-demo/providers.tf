terraform {
  required_version = ">= 1.5"
  required_providers {
    oci = {
      source  = "oracle/oci"
      version = ">= 6.0"
    }
  }
}

# 認証は ~/.oci/config の DEFAULT プロファイル(IAM署名)。jetuse-dev に閉じる。
provider "oci" {
  region = var.region
}
