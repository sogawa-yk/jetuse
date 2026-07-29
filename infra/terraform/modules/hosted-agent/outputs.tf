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
# 実体は CLI で作っているため、OCID はデータソース(ACTIVE で名前一致)から引く。
# terraform_data.agent はデプロイメントが ACTIVE になるまで待ってから完了するので、
# ここに値が入る時点で invoke 可能な状態になっている。
output "app_ocids" {
  value = {
    for k, d in data.oci_generative_ai_hosted_applications.agent :
    k => one([for item in d.hosted_application_collection[0].items : item.id])
  }
}
