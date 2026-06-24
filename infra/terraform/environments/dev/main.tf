module "network" {
  source              = "../../modules/network"
  compartment_ocid    = var.compartment_ocid
  prefix              = var.prefix
  vcn_cidr            = var.vcn_cidr
  public_subnet_cidr  = cidrsubnet(var.vcn_cidr, 8, 0)
  private_subnet_cidr = cidrsubnet(var.vcn_cidr, 8, 1)
}

module "object_storage" {
  source           = "../../modules/object-storage"
  compartment_ocid = var.compartment_ocid
  prefix           = var.prefix
}

module "adb" {
  count            = var.enable_adb ? 1 : 0
  source           = "../../modules/adb"
  compartment_ocid = var.compartment_ocid
  prefix           = var.prefix
  admin_password   = var.adb_admin_password
}

module "ocir" {
  source           = "../../modules/ocir"
  compartment_ocid = var.compartment_ocid
  prefix           = var.prefix
}

module "container_instance" {
  count                 = var.api_image_url == "" ? 0 : 1
  source                = "../../modules/container-instance"
  compartment_ocid      = var.compartment_ocid
  prefix                = var.prefix
  subnet_id             = module.network.private_subnet_id
  nsg_id                = module.network.app_nsg_id
  image_url             = var.api_image_url
  image_pull_secret_id  = var.image_pull_secret_id
  registry_username     = var.registry_username
  registry_password     = var.registry_password
  environment_variables = merge(var.api_environment, { LOG_OCID = module.observability.app_log_id })
  memory_gb             = 4 # 右サイズ(ARCH-01試算、ユーザー承認 2026-06-12)
}

module "opensearch" {
  count            = var.enable_opensearch ? 1 : 0
  source           = "../../modules/opensearch"
  compartment_ocid = var.compartment_ocid
  prefix           = var.prefix
  vcn_id           = module.network.vcn_id
  subnet_id        = module.network.private_subnet_id
  vcn_cidr         = var.vcn_cidr
}

module "functions" {
  source           = "../../modules/functions"
  compartment_ocid = var.compartment_ocid
  prefix           = var.prefix
  subnet_id        = module.network.private_subnet_id
  router_image     = var.fn_router_image
  # CIと同じ環境変数+リソースプリンシパル(fnfuncはjetuse-dgに織り込み済み)
  router_config = merge(var.api_environment, {
    AUTH_MODE = "resource_principal"
    LOG_OCID  = module.observability.app_log_id
  })
}

locals {
  # fnルーターが担当するAPIセグメント(ARCH-02第1陣)。GWはCIより特定的なルートを優先する
  fn_router_segments = ["presets", "dbchat", "tts"]
  fn_routes = module.functions.router_function_id == "" ? {} : {
    for s in local.fn_router_segments : s => module.functions.router_function_id
  }
}

module "observability" {
  source              = "../../modules/observability"
  compartment_ocid    = var.compartment_ocid
  prefix              = var.prefix
  apigw_deployment_id = module.api_gateway.deployment_id
  fnapp_id            = module.functions.application_id
}

module "api_gateway" {
  source             = "../../modules/api-gateway"
  compartment_ocid   = var.compartment_ocid
  prefix             = var.prefix
  region             = var.region
  subnet_id          = module.network.public_subnet_id
  nsg_id             = module.network.apigw_nsg_id
  ci_base_url        = length(module.container_instance) == 0 ? "" : "http://${module.container_instance[0].private_ip}:8000"
  functions_routes   = merge(var.functions_routes, local.fn_routes)
  rate_limit_rps     = var.rate_limit_rps
  spa_par_access_uri = module.object_storage.spa_par_access_uri
}

module "identity_domain" {
  count            = var.enable_identity_domain ? 1 : 0
  source           = "../../modules/identity-domain"
  providers        = { oci = oci.home }
  compartment_ocid = var.compartment_ocid
  prefix           = var.prefix
  region           = var.region
}

module "iam" {
  count            = var.enable_iam ? 1 : 0
  source           = "../../modules/iam"
  tenancy_ocid     = var.tenancy_ocid
  compartment_ocid = var.compartment_ocid
  prefix           = var.prefix
}
