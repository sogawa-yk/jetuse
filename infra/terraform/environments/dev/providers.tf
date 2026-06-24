terraform {
  required_version = ">= 1.5"
  required_providers {
    oci = {
      source  = "oracle/oci"
      version = ">= 6.0"
    }
  }
}

# 認証は ~/.oci/config の DEFAULT プロファイル(IAM署名)
provider "oci" {
  region = var.region
}

# Identity系のCREATE/UPDATE/DELETEはホームリージョン必須(403 NotAllowed対策)
provider "oci" {
  alias  = "home"
  region = var.home_region
}
