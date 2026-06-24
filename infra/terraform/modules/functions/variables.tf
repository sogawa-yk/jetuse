variable "compartment_ocid" {
  type = string
}

variable "prefix" {
  type = string
}

variable "subnet_id" {
  type = string
}

variable "router_image" {
  description = "fnルーターのOCIRイメージURL(空ならfunction未作成)"
  type        = string
  default     = ""
}

variable "router_config" {
  description = "fnルーターの環境変数(api_environmentと同系)"
  type        = map(string)
  default     = {}
}
