variable "compartment_ocid" {
  type = string
}

variable "prefix" {
  type = string
}

variable "spa_par_expiry" {
  description = "SPA配信用PAR(AnyObjectRead)の失効日時(RFC3339)。ADR-0004の方式A検証用"
  type        = string
  default     = "2027-12-31T00:00:00Z"
}
