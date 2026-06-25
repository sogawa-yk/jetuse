variable "compartment_ocid" {
  type = string
}

variable "prefix" {
  type = string
}

variable "repositories" {
  description = "作成するリポジトリ名(プレフィックス除く)。OCIRはpush前の事前作成必須(無いと403 — Phase 0実証)"
  type        = list(string)
  default     = ["api"]
}

variable "is_public" {
  description = "true で匿名 pull 可能。Container Instance/Functions が認証なしで取得できる(ADR-0011)"
  type        = bool
  default     = false
}
