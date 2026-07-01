terraform {
  required_version = ">= 1.5"

  required_providers {
    oci = {
      source  = "oracle/oci"
      version = ">= 6.0"
    }
  }
}

# Dynamic Group / Policy は IAM の変更なので、必ずテナンシのホームリージョンへ送る。
provider "oci" {
  region = var.home_region
}
