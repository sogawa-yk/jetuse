# 管理者が対象コンパートメントごとに一度だけ適用する IAM bootstrap。
# JetUse アプリ本体は infra/orm から、テナンシ管理権限を持たない利用者が適用できる。
module "iam" {
  source = "../terraform/modules/iam"

  tenancy_ocid           = var.tenancy_ocid
  compartment_ocid       = var.compartment_ocid
  prefix                 = var.prefix
  enable_semantic_store  = var.enable_semantic_store
  create_deployer_policy = var.create_deployer_policy
  deployer_group_subject = var.deployer_group_subject
}
