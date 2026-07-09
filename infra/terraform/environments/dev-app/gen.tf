# --- SP3-08: フロント生成の実 Container Instance 化(ADR-0023 §1 決定 B')。 ---
# SP3-07 の main.tf と分離した追加ファイル(コンフリクト最小化 — tasks/SP3-08 作業内容3)。
# 生成 CI 自体は API がジョブごとに作成/削除する(使い捨て)。tf が持つのは:
#   1. 生成/ビルドイメージの OCIR repo(public — CI が匿名 pull。ADR-0011。イメージに秘密なし)
#   2. API へ配線する env(local.generation_env — main.tf の merge で API CI へ渡す)
# イメージの build/push は ops/deploy-dev-app.sh gen-images(タグは var.generation_image_tag)。

module "gen_ocir" {
  source           = "../../modules/ocir"
  compartment_ocid = var.compartment_ocid
  prefix           = "jetuse-dev"
  repositories     = ["gen", "build"]
  is_public        = true
}

data "oci_identity_availability_domains" "gen" {
  compartment_id = var.compartment_ocid
}

data "oci_objectstorage_namespace" "gen" {
  compartment_id = var.compartment_ocid
}

# OCIR ホストは region キーから導出(エンドポイント実値をコミットしない — SP3-07 と同じ規律)
data "oci_identity_regions" "all" {}

locals {
  ocir_host = format("%s.ocir.io", lower(
    [for r in data.oci_identity_regions.all.regions : r.key if r.name == var.region][0]
  ))
  gen_image_repo = "${local.ocir_host}/${data.oci_objectstorage_namespace.gen.namespace}"
  # API CI へ渡す生成 runtime 配線(secrets なし。PAR/資格情報は runtime が都度発行/不使用)
  generation_env = {
    GENERATION_RUNTIME            = "oci-ci"
    GENERATION_CI_SUBNET_OCID     = module.network.private_subnet_id
    GENERATION_CI_AD              = data.oci_identity_availability_domains.gen.availability_domains[0].name
    GENERATION_GEN_IMAGE_URL      = "${local.gen_image_repo}/jetuse-dev-gen:${var.generation_image_tag}"
    GENERATION_BUILD_IMAGE_URL    = "${local.gen_image_repo}/jetuse-dev-build:${var.generation_image_tag}"
    GENERATION_CI_GEN_TIMEOUT_S   = tostring(var.generation_ci_gen_timeout_s)
    GENERATION_CI_BUILD_TIMEOUT_S = tostring(var.generation_ci_build_timeout_s)
  }
}

variable "generation_image_tag" {
  description = "生成/ビルドイメージのタグ(ops/deploy-dev-app.sh gen-images の既定と一致させる)。opencode 版 + 相スクリプト改訂(-rN)でタグを進める(タグは不変 — 内容を変えたら必ず進める)"
  type        = string
  default     = "oc1.17.15-r2"
}

variable "generation_ci_gen_timeout_s" {
  description = "相1(生成 CI)のタイムアウト秒(ADR-0023 §1 既定 9 分)"
  type        = number
  default     = 540
}

variable "generation_ci_build_timeout_s" {
  description = "相2(信頼ビルド CI)のタイムアウト秒(ADR-0023 §1 既定 2 分)"
  type        = number
  default     = 120
}
