variable "compartment_ocid" {
  type = string
}

variable "prefix" {
  type = string
}

variable "vcn_cidr" {
  type    = string
  default = "10.1.0.0/16"
}

variable "public_subnet_cidr" {
  type    = string
  default = "10.1.0.0/24"
}

variable "private_subnet_cidr" {
  type    = string
  default = "10.1.1.0/24"
}

variable "app_port" {
  description = "FastAPI(Container Instance)の待受ポート"
  type        = number
  default     = 8000
}
