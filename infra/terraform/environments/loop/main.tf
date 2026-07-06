# ループ完了ゲート実環境 E2E 用の再利用 ADB「jetuse-loop-adb」。
# 2026-06-25 作成(PLG-02) → 2026-07-05 リポジトリリセットで旧 jetuse-dev コンパートメントごと
# アクセス不能化(STOPPED のまま残存・API権限なし) → 2026-07-06 SP1-02 で新レイアウト jetuse/dev へ再作成。
# db_name はテナンシ内一意制約で旧 "jetuseloop" が使えないため "jetuseloop2"。
# 旧 ADB の削除は人間ゲート(旧コンパートメントの権限が必要)。秘匿値は TF_VAR_ 注入・state は非コミット。
#
# 注: loop E2E 用 GenAI プロジェクト jetuse-loop-project(jetuse/dev、ap-osaka-1 / us-chicago-1 各1)は
# oracle/oci provider に GenerativeAiProject リソースが未実装(8.20.0 時点)のため Terraform 管理外。
# OCI CLI `oci generative-ai generative-ai-project` で管理し、provider 対応後にここへ import する。
terraform {
  required_providers {
    oci = {
      source  = "oracle/oci"
      version = ">= 6.0.0"
    }
  }
}

provider "oci" {}

variable "compartment_ocid" { type = string }
variable "admin_password" {
  type      = string
  sensitive = true
}

resource "oci_database_autonomous_database" "this" {
  compartment_id          = var.compartment_ocid
  db_name                 = "jetuseloop2"
  display_name            = "jetuse-loop-adb"
  db_version              = "26ai"
  db_workload             = "OLTP"
  compute_model           = "ECPU"
  compute_count           = 2
  data_storage_size_in_gb = 20
  is_auto_scaling_enabled = false
  license_model           = "LICENSE_INCLUDED"
  admin_password          = var.admin_password
}

output "adb_ocid" { value = oci_database_autonomous_database.this.id }
