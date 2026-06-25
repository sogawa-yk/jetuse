terraform {
  required_version = ">= 1.5"
  required_providers {
    oci = {
      source  = "oracle/oci"
      version = ">= 6.0"
    }
    # 読取 PAR の失効を apply 時刻からの相対(offset)で確定し、固定日付の劣化を避ける。
    time = {
      source  = "hashicorp/time"
      version = ">= 0.9"
    }
  }
}
