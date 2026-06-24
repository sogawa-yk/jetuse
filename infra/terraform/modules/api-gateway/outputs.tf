output "gateway_id" {
  value = oci_apigateway_gateway.this.id
}

output "endpoint" {
  value = oci_apigateway_gateway.this.hostname
}

output "deployment_id" {
  value = oci_apigateway_deployment.this.id
}
