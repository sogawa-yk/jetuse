# jetuse/dev への SP2 プレビュー配備（API Gateway + Object Storage 構成）。
# 既存モジュールを合成した自己完結スタック。secret は tfvars / TF_VAR_ 注入・state は非コミット。
# 本番へは同じモジュール構成で tfvars を差し替えて promote する。
terraform {
  required_providers {
    oci = {
      source  = "oracle/oci"
      version = ">= 6.0.0"
    }
  }
}

provider "oci" {}
