variable "compartment_ocid" {
  type = string
}

variable "prefix" {
  type = string
}

variable "spa_par_expiry" {
  description = "SPA配信PAR(AnyObjectRead)の失効日時(RFC3339)。空ならapply時刻起点+1年の相対期限"
  type        = string
  default     = ""
}
