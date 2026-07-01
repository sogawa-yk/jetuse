output "runtime_dynamic_group" {
  description = "Container Instances / Functions 用 Dynamic Group"
  value       = module.iam.runtime_dynamic_group
}

output "adb_dynamic_group" {
  description = "Autonomous Database resource principal 用 Dynamic Group"
  value       = module.iam.adb_dynamic_group
}

output "semantic_store_dynamic_group" {
  description = "Semantic Store 用 Dynamic Group（有効時）"
  value       = module.iam.semantic_store_dynamic_group
}

output "deployer_policy_id" {
  description = "通常デプロイ担当グループへ付与した Policy（有効時）"
  value       = module.iam.deployer_policy_id
}

output "next_step" {
  value = "IAM の反映を数分待ってから、Deploy to Oracle Cloud で infra/orm を選び、同じ compartment（prefix は同じ値を推奨）を指定してください。"
}
