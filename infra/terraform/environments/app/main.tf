# 開発者ごとのアプリ・スタック(Container Instance + API Gateway + SPAバケット)。
# 高価な共有基盤(VCN/ADB/Identity Domain/OCIR等)は environments/dev が作り、
# ここはその出力を terraform_remote_state(localバックエンド) で参照するだけ。
# state はper-devで分ける: terraform apply -var-file=<dev>.tfvars -state=<dev>.tfstate

data "terraform_remote_state" "shared" {
  backend = "local"
  config  = { path = var.shared_state_path }
}

locals {
  prefix           = "jetuse-${var.dev_name}"
  compartment_ocid = data.terraform_remote_state.shared.outputs.compartment_ocid
}

module "container_instance" {
  source               = "../../modules/container-instance"
  compartment_ocid     = local.compartment_ocid
  prefix               = local.prefix
  subnet_id            = data.terraform_remote_state.shared.outputs.private_subnet_id
  nsg_id               = data.terraform_remote_state.shared.outputs.app_nsg_id
  image_url            = var.api_image_url
  image_pull_secret_id = var.image_pull_secret_id
  registry_username    = var.registry_username
  registry_password    = var.registry_password
  memory_gb            = var.memory_gb
  # 本人スキーマ・認証・ログだけ上書き。残りは api_environment(共有値+本人DBパスワード)から
  environment_variables = merge(var.api_environment, {
    AUTH_REQUIRED  = var.auth_required ? "true" : "false"
    ADB_USER       = var.adb_user
    ADB_QUERY_USER = var.adb_query_user
    LOG_OCID       = data.terraform_remote_state.shared.outputs.app_log_id
  })
}

module "spa" {
  source           = "../../modules/spa-bucket"
  compartment_ocid = local.compartment_ocid
  prefix           = local.prefix
}

# 本人専用の API Gateway NSG。auth_required=false の公開dev環境を絞るため
# 443 の許可元を apigw_allow_cidr で制御する(共有の全開放NSGは使わない)。
resource "oci_core_network_security_group" "apigw" {
  compartment_id = local.compartment_ocid
  vcn_id         = data.terraform_remote_state.shared.outputs.vcn_id
  display_name   = "${local.prefix}-nsg-apigw"
}

resource "oci_core_network_security_group_security_rule" "apigw_https_in" {
  network_security_group_id = oci_core_network_security_group.apigw.id
  direction                 = "INGRESS"
  protocol                  = "6"
  source_type               = "CIDR_BLOCK"
  source                    = var.apigw_allow_cidr
  tcp_options {
    destination_port_range {
      min = 443
      max = 443
    }
  }
}

resource "oci_core_network_security_group_security_rule" "apigw_egress" {
  network_security_group_id = oci_core_network_security_group.apigw.id
  direction                 = "EGRESS"
  protocol                  = "all"
  destination_type          = "CIDR_BLOCK"
  destination               = "0.0.0.0/0"
}

module "api_gateway" {
  source           = "../../modules/api-gateway"
  compartment_ocid = local.compartment_ocid
  prefix           = local.prefix
  region           = var.region
  subnet_id        = data.terraform_remote_state.shared.outputs.public_subnet_id
  nsg_id           = oci_core_network_security_group.apigw.id
  ci_base_url      = "http://${module.container_instance.private_ip}:8000"
  # dev環境はFunctionsを使わず全 /api を本人のCIで処理(キャッチオール /api/{p*})
  functions_routes   = {}
  rate_limit_rps     = var.rate_limit_rps
  spa_par_access_uri = module.spa.spa_par_access_uri
}
