output "apigw_hostname" {
  value = module.api_gateway.endpoint
}

output "spa_bucket" {
  value = module.spa.spa_bucket
}

output "api_private_ip" {
  value = module.container_instance.private_ip
}
