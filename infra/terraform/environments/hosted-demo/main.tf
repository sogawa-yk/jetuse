# DEP-01: 生成デモのコンテナ配備(L3 ホスト型)。
# 既存の container-instance モジュール(ADR-0011 で OCIR を参照)を **そのまま再利用** し、
# deploy.py が生成した配備仕様(prefix/image/非秘密 env)を流し込むだけ。新規インフラの
# プロビジョニングはしない(D8: デプロイ上限=コンテナ)。push/apply は人間ゲート(plan 止まり)。

# コンテナへ渡す env(Terraform 管理)は **非秘密のみ**:
#   - **静的 env**(deploy.py 生成・committed な tfvars。キーは OCI_REGION か JETUSE_*)。
#   - **DEP-02 のベース URL 注入**(`JETUSE_PLATFORM_API_BASE_URL`。非秘密)。
# **短期トークンは Terraform に渡さない**。Terraform に渡した値は resource 入力として
# **state へ保存される**(`sensitive` は CLI 表示のマスクのみで state には残る)。短期トークンを state に
# 残さないため、トークンは Terraform 経路ではなく **起動時のアウトオブバンド注入**(オーケストレータが
# `deploy_inject.build_runtime_injection().secret_env()` を実行中コンテナへ直接注入 / 将来はコンテナ自身が
# ブローカーから取得)で渡す(ADR-0016 §4・§5)。DB 認証情報は注入経路に存在しない(D5)。
locals {
  # DEP-02: ベース URL の注入のみ(非秘密。state に残っても秘密漏えいにならない)。
  # 値が空なら載せない(注入無効化時に空文字 env を作らない)。
  platform_runtime_env = var.platform_api_base_url == "" ? {} : {
    JETUSE_PLATFORM_API_BASE_URL = var.platform_api_base_url
  }
  # 静的(非秘密)env に非秘密のベース URL を上書き合流。**短期トークンはここに含めない**。
  effective_environment = merge(var.environment_variables, local.platform_runtime_env)
}

module "container_instance" {
  source = "../../modules/container-instance"

  compartment_ocid = var.compartment_ocid
  prefix           = var.prefix
  subnet_id        = var.subnet_id
  nsg_id           = var.nsg_id
  image_url        = var.image_url
  app_port         = var.app_port
  ocpus            = var.ocpus
  memory_gb        = var.memory_gb
  # 非秘密の静的 env ＋ 非秘密のベース URL のみ(短期トークンは Terraform を通さない=state に残さない)。
  environment_variables = local.effective_environment
  # image_pull_secret は渡さない(ADR-0011: public OCIR=認証なし pull)。module 既定 "" を使う。
}
