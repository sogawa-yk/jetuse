variable "compartment_ocid" {
  type = string
}

variable "prefix" {
  type = string
}

variable "region" {
  type = string
}

variable "home_region" {
  description = "テナンシのホームリージョン(Identity Domain 作成先)。空なら region にフォールバック"
  type        = string
  default     = ""
}
