# 非ストリーミングAPI群の置き場(ADR-0005)。個々のfunctionはAPP-01以降でデプロイ
resource "oci_functions_application" "this" {
  compartment_id = var.compartment_ocid
  display_name   = "${var.prefix}-fnapp"
  subnet_ids     = [var.subnet_id]
  shape          = "GENERIC_X86"
}

# 非ストリーミングAPIルーター(ARCH-02)。fn/router/func.py のイメージ
resource "oci_functions_function" "router" {
  count              = var.router_image == "" ? 0 : 1
  application_id     = oci_functions_application.this.id
  display_name       = "${var.prefix}-fn-router"
  image              = var.router_image
  memory_in_mbs      = 512
  timeout_in_seconds = 120
  config             = var.router_config
}
