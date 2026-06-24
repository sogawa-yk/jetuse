# OPS-02: OCI Loggingへの集約(アプリのカスタムログ + GW/Fnのサービスログ)
resource "oci_logging_log_group" "this" {
  compartment_id = var.compartment_ocid
  display_name   = "${var.prefix}-logs"
}

# アプリ(CI/Fn)が直接ingestionするカスタムログ(jetuse_core/obs.py)
resource "oci_logging_log" "app" {
  display_name       = "${var.prefix}-app"
  log_group_id       = oci_logging_log_group.this.id
  log_type           = "CUSTOM"
  is_enabled         = true
  retention_duration = 30
}

resource "oci_logging_log" "apigw_execution" {
  display_name       = "${var.prefix}-apigw-execution"
  log_group_id       = oci_logging_log_group.this.id
  log_type           = "SERVICE"
  is_enabled         = true
  retention_duration = 30
  configuration {
    compartment_id = var.compartment_ocid
    source {
      category    = "execution"
      resource    = var.apigw_deployment_id
      service     = "apigateway"
      source_type = "OCISERVICE"
    }
  }
}

resource "oci_logging_log" "apigw_access" {
  display_name       = "${var.prefix}-apigw-access"
  log_group_id       = oci_logging_log_group.this.id
  log_type           = "SERVICE"
  is_enabled         = true
  retention_duration = 30
  configuration {
    compartment_id = var.compartment_ocid
    source {
      category    = "access"
      resource    = var.apigw_deployment_id
      service     = "apigateway"
      source_type = "OCISERVICE"
    }
  }
}

# Functionsのinvokeログ(stdout/stderr=アプリのJSON Linesも乗る)
resource "oci_logging_log" "fn_invoke" {
  display_name       = "${var.prefix}-fn-invoke"
  log_group_id       = oci_logging_log_group.this.id
  log_type           = "SERVICE"
  is_enabled         = true
  retention_duration = 30
  configuration {
    compartment_id = var.compartment_ocid
    source {
      category    = "invoke"
      resource    = var.fnapp_id
      service     = "functions"
      source_type = "OCISERVICE"
    }
  }
}
