# DEP-01: 生成デモのコンテナ配備(L3 ホスト型)。
# 既存の container-instance モジュール(ADR-0011 で OCIR を参照)を **そのまま再利用** し、
# deploy.py が生成した配備仕様(prefix/image/非秘密 env)を流し込むだけ。新規インフラの
# プロビジョニングはしない(D8: デプロイ上限=コンテナ)。push/apply は人間ゲート(plan 止まり)。

# コンテナへ渡す env は **非秘密のみ**(キーは OCI_REGION か JETUSE_*)。秘密(Vault OCID)は本環境で
# 一切扱わない=Terraform state に機微な参照先を残さない。秘密の解決・注入は DEP-02(Platform API 注入)。
module "container_instance" {
  source = "../../modules/container-instance"

  compartment_ocid      = var.compartment_ocid
  prefix                = var.prefix
  subnet_id             = var.subnet_id
  nsg_id                = var.nsg_id
  image_url             = var.image_url
  app_port              = var.app_port
  ocpus                 = var.ocpus
  memory_gb             = var.memory_gb
  environment_variables = var.environment_variables
  # image_pull_secret は渡さない(ADR-0011: public OCIR=認証なし pull)。module 既定 "" を使う。
}
