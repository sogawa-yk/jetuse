# private_ip 等は環境依存の実値(エンドポイント)。リポジトリ方針に従い plan/apply ログに素で
# 出さないよう sensitive 指定する(コミット・ログ流出の予防)。
output "instance_id" {
  value     = module.container_instance.instance_id
  sensitive = true
}

output "private_ip" {
  value     = module.container_instance.private_ip
  sensitive = true
}
