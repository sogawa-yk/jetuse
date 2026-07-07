locals {
  prefix = var.prefix
}

# VCN + public/private サブネット + IGW/NAT/SGW + NSG（apigw=443 from any / app=app_port from VCN）
module "network" {
  source              = "../../modules/network"
  compartment_ocid    = var.compartment_ocid
  prefix              = local.prefix
  vcn_cidr            = var.vcn_cidr
  public_subnet_cidr  = var.public_subnet_cidr
  private_subnet_cidr = var.private_subnet_cidr
  app_port            = var.app_port
}

# SPA 静的ホスティング（非公開バケット + AnyObjectRead の PAR）
module "spa" {
  source           = "../../modules/spa-bucket"
  compartment_ocid = var.compartment_ocid
  prefix           = local.prefix
}

# バックエンド（SP2 API イメージ）。private サブネットで起動、DB は再利用 ADB。
module "container_instance" {
  source           = "../../modules/container-instance"
  compartment_ocid = var.compartment_ocid
  prefix           = local.prefix
  subnet_id        = module.network.private_subnet_id
  nsg_id           = module.network.app_nsg_id
  image_url        = var.image_url
  app_port         = var.app_port
  ocpus            = var.ocpus
  memory_gb        = var.memory_gb
  # プレビューは認証オフ。region/compartment は上位変数から導出、残りはフラット変数から。
  environment_variables = {
    AUTH_MODE           = var.auth_mode
    AUTH_REQUIRED       = "false"
    OCI_REGION          = var.region
    COMPARTMENT_OCID    = var.compartment_ocid
    PROJECT_OCID        = var.project_ocid
    OS_NAMESPACE        = var.os_namespace
    ADB_OCID            = var.adb_ocid
    ADB_DSN             = var.adb_dsn
    ADB_USER            = var.adb_user
    ADB_PASSWORD        = var.adb_password
    ADB_QUERY_USER      = var.adb_query_user
    ADB_QUERY_PASSWORD  = var.adb_query_password
    ADB_WALLET_PASSWORD = var.adb_wallet_password
  }
  registry_username = var.registry_username
  registry_password = var.registry_password
}

# 単一オリジンの玄関: /api/* → CI、/* → SPA バケット（PAR 経由）。apigw NSG は 443 を公開。
module "api_gateway" {
  source             = "../../modules/api-gateway"
  compartment_ocid   = var.compartment_ocid
  prefix             = local.prefix
  region             = var.region
  subnet_id          = module.network.public_subnet_id
  nsg_id             = module.network.apigw_nsg_id
  ci_base_url        = "http://${module.container_instance.private_ip}:${var.app_port}"
  functions_routes   = {}
  spa_par_access_uri = module.spa.spa_par_access_uri
  rate_limit_rps     = 0
}

output "gateway_url" {
  value = "https://${module.api_gateway.endpoint}"
}

output "spa_bucket" {
  value = module.spa.spa_bucket
}

output "ci_private_ip" {
  value = module.container_instance.private_ip
}
