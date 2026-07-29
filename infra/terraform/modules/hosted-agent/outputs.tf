# IDCS アプリの name 属性が OAuth の client_id。
output "client_id" {
  value = oci_identity_domains_app.agent.name
}

output "client_secret" {
  value     = oci_identity_domains_app.agent.client_secret
  sensitive = true
}

# token 要求で使うのは短いスコープ名ではなく完全修飾スコープ(fqs = "<audience><scope>")。
# provider が computed で返す値をそのまま使う(手で連結すると IDCS 側の規則変更に追随できない)。
output "scope" {
  value = one([for s in oci_identity_domains_app.agent.scopes : s.fqs])
}

# SDK キー -> Hosted Application OCID。呼び出し元が AGENT_<SDK>_APP_OCID へ配線する。
# デプロイメントが ACTIVE になる前に OCID を配ると invoke が失敗するため、
# デプロイ完了に依存させてから出す。
output "app_ocids" {
  value = {
    for k, app in oci_generative_ai_hosted_application.agent :
    k => app.id
  }
  depends_on = [oci_generative_ai_hosted_deployment.agent]
}
