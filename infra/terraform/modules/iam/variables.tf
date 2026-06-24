variable "tenancy_ocid" {
  description = "動的グループはテナンシ直下に作成される"
  type        = string
}

variable "compartment_ocid" {
  type = string
}

variable "prefix" {
  type = string
}
