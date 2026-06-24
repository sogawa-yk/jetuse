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
  }
}

# Resource Manager がプリンシパル認証を注入する。region は schema.yaml の hidden 変数(${region})。
provider "oci" {
  region = var.region
}

# Identity系のCREATEはホームリージョン必須
provider "oci" {
  alias  = "home"
  region = var.home_region
}
