variable "compartment_ocid" {
  type = string
}

variable "prefix" {
  type = string
}

variable "subnet_id" {
  type = string
}

variable "nsg_id" {
  type = string
}

variable "image_url" {
  description = "OCIRイメージ(例: kix.ocir.io/<ns>/jetuse-dev-api:tag)"
  type        = string
}

variable "app_port" {
  type    = number
  default = 8000
}

variable "ocpus" {
  type    = number
  default = 1
}

variable "memory_gb" {
  type    = number
  default = 8
}

variable "environment_variables" {
  type      = map(string)
  default   = {}
  sensitive = true
}

variable "image_pull_secret_id" {
  description = "OCIRがprivateの場合のVault secret OCID(任意)"
  type        = string
  default     = ""
}

variable "registry_username" {
  description = "OCIR BASIC認証ユーザー({namespace}/{user})。image_pull_secret_idより簡易な代替"
  type        = string
  default     = ""
}

variable "registry_password" {
  description = "OCIR authトークン"
  type        = string
  default     = ""
  sensitive   = true
}
