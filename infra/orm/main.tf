# INFRA-03: OCI Resource Manager ワンクリックスタック。
# environments/dev の配線を流用しつつ、ワンクリック向けに自己完結(自動パスワード生成・
# IAM/Identity Domain/OIDCアプリ/SPA配信を内包)。モジュールは ../terraform/modules を参照。

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
}

module "adb" {
  source           = "../terraform/modules/adb"
  compartment_ocid = var.compartment_ocid
  prefix           = var.prefix
  admin_password   = local.adb_admin_password
  # ウォレットをTerraformで生成し、base64テキストでバケットへ配置する(コンテナはobject readのみでOK)
  generate_wallet = true
  wallet_password = random_password.wallet.result
}

module "ocir" {
  source           = "../terraform/modules/ocir"
  compartment_ocid = var.compartment_ocid
  prefix           = var.prefix
}

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
  router_image     = var.fn_router_image
  router_config = merge(local.api_environment, {
    AUTH_MODE = "resource_principal"
    LOG_OCID  = module.observability.app_log_id
  })
}

module "container_instance" {
  source                = "../terraform/modules/container-instance"
  compartment_ocid      = var.compartment_ocid
  prefix                = var.prefix
  subnet_id             = module.network.private_subnet_id
  nsg_id                = module.network.app_nsg_id
  image_url             = var.api_image_url
  environment_variables = merge(local.api_environment, { LOG_OCID = module.observability.app_log_id })
  memory_gb             = 4
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
}

module "identity_domain_app" {
  count         = var.enable_auth ? 1 : 0
  source        = "../terraform/modules/identity-domain-app"
  prefix        = var.prefix
  idcs_endpoint = module.identity_domain[0].domain_url
  redirect_uri  = "https://${module.api_gateway.endpoint}/"
  demo_email    = var.demo_email
  demo_password = random_password.demo.result
}

module "iam" {
  count            = var.enable_iam ? 1 : 0
  source           = "../terraform/modules/iam"
  tenancy_ocid     = var.tenancy_ocid
  compartment_ocid = var.compartment_ocid
  prefix           = var.prefix
}
