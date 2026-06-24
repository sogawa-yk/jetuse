variable "compartment_ocid" {
  type = string
}

variable "prefix" {
  type = string
}

variable "spa_par_expiry" {
  description = "SPA配信PAR(AnyObjectRead)の失効日時(RFC3339)。dev環境は長めに"
  type        = string
  default     = "2027-12-31T00:00:00Z"
}
