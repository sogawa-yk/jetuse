variable "compartment_ocid" {
  type = string
}

variable "prefix" {
  type = string
}

variable "spa_par_expiry" {
  description = "SPA配信用PAR(AnyObjectRead)の失効日時(RFC3339)。空ならapply時刻起点+1年の相対期限(可搬性: 固定絶対日付は将来のdeployで最初から失効するため)"
  type        = string
  default     = ""
}
