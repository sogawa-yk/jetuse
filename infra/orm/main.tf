# INFRA-03: OCI Resource Manager ワンクリックスタック。
# environments/dev の配線を流用しつつ、ワンクリック向けに自己完結(自動パスワード生成・
# IAM/Identity Domain/OIDCアプリ/SPA配信を内包)。モジュールは ../terraform/modules を参照。

# 対応リージョンチェック(ADR-0017)。対応外だとイメージ pull / CreateFunction が
# apply 途中で不可解に失敗するため、plan 時に明示エラーで止める。
resource "terraform_data" "region_guard" {
  lifecycle {
    precondition {
      condition     = contains(local.ocir_supported_region_keys, local.deploy_region_key) || (var.api_image_url != "" && var.fn_router_image != "")
      error_message = "JetUse のワンクリックデプロイは ap-osaka-1 / ap-tokyo-1 / us-ashburn-1 / us-chicago-1 のみ対応です(コンテナイメージの事前push先)。他リージョンでは、イメージを自リージョンOCIRへミラーし api_image_url と fn_router_image の両方を指定してください。"
    }
    # GenAI(推論+agentic API)の対応リージョンは OCIR より狭い。実証済は kix/ord のみで、
    # nrt/iad は apply が通っても GenAI が動かず RAG/会話/生成が全滅する。イメージのミラー
    # (api_image_url 指定)では回避できないため、未検証リージョンは明示オプトインを要求する。
    precondition {
      condition     = var.allow_unvalidated_genai_region || contains(local.genai_validated_region_keys, local.deploy_region_key)
      error_message = "リージョン ${var.region}(key=${local.deploy_region_key}) は GenAI(推論/agentic API)が未検証です。JetUse の RAG/会話メモリ/デモ生成は GenAI に依存し、実証済は 大阪(kix)/シカゴ(ord) のみです。デプロイ前に対象リージョンの GenAI/agentic API の提供状況を確認し、承知の上で進める場合は allow_unvalidated_genai_region=true を設定してください。"
    }
    # ocir_namespace を公開既定から変えると、api_image_url/fn_router_image が空のままでは
    # auto-synth が <region>.ocir.io/<変更後namespace>/jetuse-{api,fn-router}:latest を pull しようとし、
    # そのイメージが存在せず Container Instance / Functions が起動しない(サイレント)。
    # よって公開既定以外にする場合は両イメージURLの明示指定を必須にする(自テナンシへミラーした前提)。
    precondition {
      condition     = var.ocir_namespace == local.public_ocir_namespace || (var.api_image_url != "" && var.fn_router_image != "")
      error_message = "ocir_namespace を公開既定(${local.public_ocir_namespace})から変更する場合は、イメージを自テナンシの OCIR へミラーした上で api_image_url と fn_router_image を両方明示してください。ocir_namespace 単独の変更では jetuse-api/jetuse-fn-router の pull に失敗します(これは自テナンシの Object Storage namespace ではありません)。"
    }
    # ホスト型エージェント(PORT-03)はコンテナ自身が resource principal で GenAI / Object Storage /
    # ADB を呼ぶため、Dynamic Group に generativeaihostedapplication / generativeaihosteddeployment が
    # 含まれ、その DG にランタイム権限が付いている必要がある。スタックが IAM を作らない運用では
    # それを保証できず、配備は成功しても invoke が必ず権限エラーになる。黙って壊れるより plan で止める。
    precondition {
      condition     = !local.hosted_agents_enabled || (var.enable_dynamic_group && var.enable_runtime_policy) || var.existing_iam_covers_hosted_agents
      error_message = "ホスト型エージェント(enable_hosted_agents=true)は、スタックが Dynamic Group と Runtime Policy を作る構成を前提にしています。既存IAMを流用する場合は、既存 Dynamic Group に generativeaihostedapplication と generativeaihosteddeployment を追加し、既存 policy が JetUse ランタイム権限(generative-ai-family / objects / autonomous-database-family 等)を与えていることを確認したうえで existing_iam_covers_hosted_agents=true を設定してください。エージェントが不要なら enable_hosted_agents=false にしてください。"
    }
    # ocir_namespace を公開既定から変えた場合、エージェント画像 3 つも自テナンシへミラー
    # されている保証が無い。api_image_url / fn_router_image と同じく明示指定を要求する。
    precondition {
      condition     = !local.hosted_agents_enabled || var.ocir_namespace == local.public_ocir_namespace || var.hosted_agent_image_registry != ""
      error_message = "ocir_namespace を公開既定(${local.public_ocir_namespace})から変更してホスト型エージェントを使う場合は、jetuse-agent-{openai,langgraph,adk} も自テナンシの OCIR へミラーしたうえで hosted_agent_image_registry(例 ${local.deploy_region_key}.ocir.io/<namespace>)を明示してください。エージェントが不要なら enable_hosted_agents=false にしてください。"
    }
  }
}

# --- 自動生成パスワード(Oracle/IDCS規則: 英大小+数字+記号, " を含めない) ---
resource "random_password" "adb_admin" {
  length           = 20
  min_upper        = 2
  min_lower        = 2
  min_numeric      = 2
  min_special      = 1
  override_special = "#_-"
}
resource "random_password" "wallet" {
  length           = 20
  min_upper        = 2
  min_lower        = 2
  min_numeric      = 2
  min_special      = 1
  override_special = "#_-"
}
resource "random_password" "jetuse_app" {
  length           = 20
  min_upper        = 2
  min_lower        = 2
  min_numeric      = 2
  min_special      = 1
  override_special = "#_-"
}
resource "random_password" "jetuse_query" {
  length           = 20
  min_upper        = 2
  min_lower        = 2
  min_numeric      = 2
  min_special      = 1
  override_special = "#_-"
}
resource "random_password" "demo" {
  length           = 16
  min_upper        = 2
  min_lower        = 2
  min_numeric      = 2
  min_special      = 1
  override_special = "#_-"

  # 旧実装(SCIMのUserへ直接passwordを書く方式)で作られた既存 Stack を更新すると、demo ユーザーは
  # 「同じパスワードが履歴に入っている」状態のまま mustChange=true で残る。UserPasswordChanger は
  # 同一パスワードの再設定を pwdpolicyViolation で拒否するため、値を変えないと更新が通らないうえ、
  # 通っても mustChange が外れずログインできないままになる。keepers を進めて**更新時に必ず
  # 新しいパスワードを発行**し、出力(demo_password)にも反映させる(FIX-58)。
  # demo_password_version を変えると新しいパスワードを発行し直す。パスワード履歴と衝突して
  # provisioner が失敗したとき、Resource Manager の変数画面から復旧できる逃げ道でもある。
  keepers = {
    password_setter = "user-password-changer-v1"
    version         = var.demo_password_version
  }
}

# IAMもアプリ本体と同じResource Manager stackで管理する。
# 実行者の権限と既存IAMに応じて、Dynamic Groupとruntime policyを個別に切り替える。
module "iam" {
  source    = "../terraform/modules/iam"
  providers = { oci = oci.home }

  tenancy_ocid              = var.tenancy_ocid
  compartment_ocid          = var.compartment_ocid
  prefix                    = var.prefix
  enable_dynamic_group      = var.enable_dynamic_group
  enable_runtime_policy     = var.enable_runtime_policy
  enable_semantic_store     = var.enable_semantic_store
  enable_project_autocreate = var.enable_project_autocreate
  create_deployer_policy    = false
  # ホスト型エージェントを配備するときだけ runtime DG にホスト型リソースを含める(PORT-03)。
  include_hosted_agent_principals = local.hosted_agents_enabled

  existing_dynamic_group = var.existing_dynamic_group
}

module "network" {
  source              = "../terraform/modules/network"
  compartment_ocid    = var.compartment_ocid
  prefix              = var.prefix
  public_subnet_cidr  = "10.1.0.0/24"
  private_subnet_cidr = "10.1.1.0/24"
}

module "object_storage" {
  source           = "../terraform/modules/object-storage"
  compartment_ocid = var.compartment_ocid
  prefix           = var.prefix
  region           = var.region
}

module "adb" {
  source           = "../terraform/modules/adb"
  compartment_ocid = var.compartment_ocid
  prefix           = var.prefix
  admin_password   = local.adb_admin_password
  db_version       = var.adb_db_version
  ecpu_count       = var.adb_ecpu_count
  # ウォレットをTerraformで生成し、base64テキストでバケットへ配置する(コンテナはobject readのみでOK)
  generate_wallet = true
  wallet_password = random_password.wallet.result

  depends_on = [module.iam]
}

# OCIRリポジトリ(jetuse-api / jetuse-fn-router)はスタックでは作らない(ADR-0011, 2026-06-25)。
# 本番用コンパートメント(genu-proto)に人間が手動で public 作成・管理する。
# 理由: (1) OCIRのrepo名はネームスペース内で一意。stackがjetuse-devに同名repoを作ると衝突する。
#       (2) イメージパスはネームスペースベース(kix.ocir.io/<namespace>/<repo>)でコンパートメント非依存
#           なので、genu-proto に置いても locals.tf のイメージURLはそのまま機能する。
#       (3) push(release.yml)は repo 事前作成済みなら通る(無いとOCIRがルートに作成を試み権限不足で失敗)。

module "observability" {
  source              = "../terraform/modules/observability"
  compartment_ocid    = var.compartment_ocid
  prefix              = var.prefix
  apigw_deployment_id = module.api_gateway.deployment_id
  fnapp_id            = module.functions.application_id
}

module "functions" {
  source           = "../terraform/modules/functions"
  compartment_ocid = var.compartment_ocid
  prefix           = var.prefix
  subnet_id        = module.network.private_subnet_id
  router_image     = local.fn_router_image
  router_config = merge(local.api_environment, {
    AUTH_MODE = "resource_principal"
    LOG_OCID  = module.observability.app_log_id
  })

  # container_instance と同じ理由(destroy 時にバケット掃除より先に止める)
  depends_on = [module.iam, module.object_storage]
}

module "container_instance" {
  source           = "../terraform/modules/container-instance"
  compartment_ocid = var.compartment_ocid
  prefix           = var.prefix
  subnet_id        = module.network.private_subnet_id
  nsg_id           = module.network.app_nsg_id
  image_url        = local.api_image_url
  # エージェントの OAuth 資格情報はここだけに渡す(Functions ルーターへは配らない)。
  environment_variables = merge(
    local.api_environment,
    local.hosted_agent_environment,
    { LOG_OCID = module.observability.app_log_id },
  )
  memory_gb = 4
  shape     = var.ci_shape

  # destroy の順序担保: モジュール全体に依存させることで、バケットの掃除(object_storage 内の
  # terraform_data.empty_buckets)より先にアプリが停止する。出力参照だけだとバケット resource に
  # しか依存せず、掃除とアプリ停止が並行して走り、掃除後に書き込まれて 409 になりうる。
  depends_on = [module.iam, module.object_storage]
}

module "opensearch" {
  count            = var.enable_opensearch ? 1 : 0
  source           = "../terraform/modules/opensearch"
  compartment_ocid = var.compartment_ocid
  prefix           = var.prefix
  vcn_id           = module.network.vcn_id
  subnet_id        = module.network.private_subnet_id
  vcn_cidr         = module.network.vcn_cidr
}

locals {
  fn_router_segments = ["presets", "dbchat", "tts"]
  fn_routes = module.functions.router_function_id == "" ? {} : {
    for s in local.fn_router_segments : s => module.functions.router_function_id
  }
}

module "api_gateway" {
  source             = "../terraform/modules/api-gateway"
  compartment_ocid   = var.compartment_ocid
  prefix             = var.prefix
  region             = var.region
  subnet_id          = module.network.public_subnet_id
  nsg_id             = module.network.apigw_nsg_id
  ci_base_url        = "http://${module.container_instance.private_ip}:8000"
  functions_routes   = local.fn_routes
  rate_limit_rps     = var.rate_limit_rps
  spa_par_access_uri = module.object_storage.spa_par_access_uri
}

module "identity_domain" {
  count            = var.enable_auth ? 1 : 0
  source           = "../terraform/modules/identity-domain"
  providers        = { oci = oci.home }
  compartment_ocid = var.compartment_ocid
  prefix           = var.prefix
  region           = var.region
  # Identity Domain はテナンシのホームリージョンにしか作れない。deployリージョンではなく
  # ホームリージョンを渡す(deployリージョン≠ホームでの作成失敗を防ぐ)。
  home_region = local.home_region
}

# IAM は作成 API が成功しても、Dynamic Group と policy の反映に実測5〜10分かかる(docs/tips.md)。
# Hosted Deployment は artifact 検証に一度失敗すると FAILED が終端状態になり、apply の再試行で
# しか復旧できない。そこで反映待ちを明示的に挟む(review F-005)。
# この待ちは module.adb(作成に10分以上)と**並行**して進むため、apply 全体の実時間は伸びない。
resource "time_sleep" "iam_propagation" {
  count = local.hosted_agents_enabled ? 1 : 0
  # 実機記録では反映に8分かかった事例がある(docs/tips.md)。既知の上限を覆う値にする。
  create_duration = "600s"

  # IAM の**内容**が変わったら待ち直す。DG 名や policy OCID は matching rule 本文や
  # statement 差し替えでは変わらないので、内容を決める入力の指紋を混ぜる。
  triggers = {
    runtime_dynamic_group = coalesce(module.iam.runtime_dynamic_group, "none")
    runtime_policy_id     = coalesce(module.iam.runtime_policy_id, "none")
    # matching rule 本文と policy statements そのもののハッシュ。変数入力だけを見ていると、
    # モジュール側のコード変更(文の追加など)で待ち直しが起きない。
    iam_content = module.iam.content_fingerprint
  }

  depends_on = [module.iam]
}

# ホスト型エージェント(PORT-03 / ADR-0019)。3SDK の ReAct コンテナを Enterprise AI Agent として
# 配備し、OAuth(client_credentials)の発行元兼リソースを同じスタックで作る。
# 配備条件は locals.hosted_agents_enabled(認証有効 かつ エージェント画像のある kix/ord)。
# min_replica=0 なので、使わない利用者にアイドル課金は発生しない。
module "hosted_agent" {
  count             = local.hosted_agents_enabled ? 1 : 0
  source            = "../terraform/modules/hosted-agent"
  compartment_ocid  = var.compartment_ocid
  prefix            = var.prefix
  region            = var.region
  idcs_endpoint     = module.identity_domain[0].domain_url
  image_registry    = local.agent_image_registry
  image_repo_prefix = var.image_repo_prefix
  image_tag         = var.image_tag
  min_replica       = var.hosted_agent_min_replica

  environment_variables = local.agent_environment

  # コンテナは resource principal で GenAI / Object Storage(ウォレット) / ADB を呼ぶ。
  # IAM(DG + policy)の**反映完了**とウォレット配置より後に作らないと、初回起動が権限エラーで落ちる。
  depends_on = [time_sleep.iam_propagation, module.object_storage, module.adb]
}

module "identity_domain_app" {
  count         = var.enable_auth ? 1 : 0
  source        = "../terraform/modules/identity-domain-app"
  prefix        = var.prefix
  idcs_endpoint = module.identity_domain[0].domain_url
  redirect_uri  = "https://${module.api_gateway.endpoint}/"
  demo_email    = var.demo_email
  demo_password = random_password.demo.result
  home_region   = local.home_region
}
