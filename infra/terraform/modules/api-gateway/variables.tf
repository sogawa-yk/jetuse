variable "compartment_ocid" {
  type = string
}

variable "prefix" {
  type = string
}

variable "region" {
  type = string
}

variable "subnet_id" {
  description = "publicサブネット"
  type        = string
}

variable "nsg_id" {
  type = string
}

variable "ci_base_url" {
  description = "SSE系バックエンド(Container Instance)の基底URL(例: http://10.1.1.x:8000)。空ならルートを作らない"
  type        = string
  default     = ""
}

variable "functions_routes" {
  description = "非ストリーミングAPIのルート。キー=パスセグメント(例 conversations)、値=function OCID(ADR-0005)"
  type        = map(string)
  default     = {}
}

variable "spa_par_access_uri" {
  description = "SPAバケットのPAR access_uri(例 /p/<token>/n/<ns>/b/<bucket>/o/)。空なら静的配信ルートを作らない(ADR-0004)"
  type        = string
  default     = ""
  sensitive   = true
}

variable "rate_limit_rps" {
  description = "SEC-03: GW全体のレート上限(req/秒。0で無効)"
  type        = number
  default     = 0
}

variable "rate_limit_key" {
  description = "レート集計キー: CLIENT_IP(送信元IP単位) or TOTAL(全体)"
  type        = string
  default     = "CLIENT_IP"
}
