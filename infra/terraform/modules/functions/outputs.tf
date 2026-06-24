output "application_id" {
  value = oci_functions_application.this.id
}

output "router_function_id" {
  value = length(oci_functions_function.router) == 0 ? "" : oci_functions_function.router[0].id
}
