variable "prefix" { type = string }
variable "compartment_ocid" { type = string }
variable "vcn_id" { type = string }
variable "subnet_id" { type = string }
variable "vcn_cidr" { type = string }

# ENH-05/SPIKE-E2: 最小構成(常設コスト抑制)。本番HAは master3+data3 等へ。
variable "software_version" {
  type    = string
  default = "2.19.1"
}
variable "master_ocpu" {
  type    = number
  default = 1
}
variable "master_memory_gb" {
  type    = number
  default = 32
}
variable "data_ocpu" {
  type    = number
  default = 2
}
variable "data_memory_gb" {
  type    = number
  default = 32
}
variable "data_storage_gb" {
  type    = number
  default = 50
}
variable "dashboard_ocpu" {
  type    = number
  default = 1
}
variable "dashboard_memory_gb" {
  type    = number
  default = 16
}
