terraform {
  required_version = ">= 1.5.0"
  required_providers {
    oci = {
      source = "oracle/oci"
      # oci_generative_ai_hosted_application / _hosted_deployment は 8.x 系で追加された。
      # ORM が入れるのは 8.24.0(2026-07-29 実測)。6.x では plan 時に unknown resource で落ちる。
      version = ">= 8.0"
    }
  }
}
