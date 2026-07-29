mock_provider "oci" {}

# PORT-03: 公開スタックが 3SDK のホスト型エージェントを配備できること。
#
# Hosted Application / Deployment は **provider ではなく OCI CLI** で作る。
# oracle/oci 8.24.0 の当該リソースは work request の完了判定を誤り、リソースが実際には
# 作成されていても必ずエラーになる（main.tf の冒頭コメント参照）。ここでは CLI 実行の
# 前提になる値（名前・OAuth 構成・作り直しの指紋）を固定する。
run "three_sdks_with_self_audience_oauth" {
  command = plan

  variables {
    compartment_ocid = "ocid1.compartment.oc1..hostedagenttest"
    prefix           = "jetuse-spike-ha01"
    region           = "us-chicago-1"
    idcs_endpoint    = "https://idcs-test.identity.oraclecloud.com"
    image_registry   = "ord.ocir.io/testnamespace"

    environment_variables = {
      OCI_REGION         = "us-chicago-1"
      COMPARTMENT_OCID   = "ocid1.compartment.oc1..hostedagenttest"
      AUTH_MODE          = "resource_principal"
      ADB_QUERY_PASSWORD = "not-a-real-password"
    }
  }

  assert {
    condition     = length(terraform_data.agent) == 3
    error_message = "All three SDK containers must be deployed by default (ADR-0019)."
  }

  # 削除は名前で引き直すため、destroy 用に持たせる input が SDK ごとに正しいこと。
  assert {
    condition = alltrue([
      for k, td in terraform_data.agent :
      td.input.display_name == "jetuse-spike-ha01-agent-${k}" &&
      td.input.region == "us-chicago-1" &&
      td.input.compartment == "ocid1.compartment.oc1..hostedagenttest"
    ])
    error_message = "Each agent must carry the identifiers its destroy-time lookup needs."
  }

  # 設定が変われば作り直す。provider 管理ではないので、この指紋が差分検出そのもの。
  assert {
    condition     = length(distinct([for td in terraform_data.agent : td.triggers_replace[0]])) == 3
    error_message = "Each SDK must have its own configuration fingerprint."
  }

  # client_credentials の自給自足構成: client 兼 resource + 自分の fqs を allowed_scopes に持つ。
  assert {
    condition = (
      oci_identity_domains_app.agent.client_type == "confidential" &&
      oci_identity_domains_app.agent.is_oauth_client == true &&
      oci_identity_domains_app.agent.is_oauth_resource == true &&
      oci_identity_domains_app.agent.audience == "jetuse-spike-ha01-agent" &&
      oci_identity_domains_app.agent.allowed_grants == toset(["client_credentials"]) &&
      contains([for s in oci_identity_domains_app.agent.allowed_scopes : s.fqs], "jetuse-spike-ha01-agentinvoke")
    )
    error_message = "The OAuth app must be a confidential client_credentials client that allows its own resource scope."
  }

  # 参照はデータソース経由（list/get のみで work request を待たないため provider バグの影響外）。
  assert {
    condition = alltrue([
      for k, d in data.oci_generative_ai_hosted_applications.agent :
      d.display_name == "jetuse-spike-ha01-agent-${k}" && d.state == "ACTIVE"
    ])
    error_message = "Application OCIDs must be resolved from the data source, filtered to ACTIVE by name."
  }
}

run "min_replica_cannot_exceed_max_replica" {
  command = plan

  variables {
    compartment_ocid = "ocid1.compartment.oc1..hostedagenttest"
    prefix           = "jetuse-spike-ha02"
    region           = "us-chicago-1"
    idcs_endpoint    = "https://idcs-test.identity.oraclecloud.com"
    image_registry   = "ord.ocir.io/testnamespace"
    min_replica      = 3
    max_replica      = 2
  }

  expect_failures = [terraform_data.agent]
}
