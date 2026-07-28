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

# destroy 時のバケット掃除(local-exec)で OCI CLI に渡すリージョン。空だと CLI 既定リージョンが
# 使われ、Terraform provider の対象リージョンと食い違うと**別リージョンを掃除して**本体の
# 削除が 409 のまま残る。呼び出し側から必ず渡すこと。
variable "region" {
  description = "バケットのリージョン(destroy時のOCI CLI呼び出しに使う。空ならCLI既定)"
  type        = string
  default     = ""
}
