mock_provider "oci" {}

# PORT-03: 公開スタックが 3SDK のホスト型エージェントを宣言的に作れること。
# ここが plan として通ること自体が重要な回帰テストになる。過去に踏んだ罠が2つある:
#  1. environment_variables は sensitive(ADB パスワードを含む)なので、素朴に for_each へ渡すと
#     「Sensitive values cannot be used as for_each arguments」で plan が停止する。
#  2. provider は value を JSON としてしか受け付けないが中身をそのまま送るため、
#     スカラーを渡すと引用符ごとコンテナに届く(2026-07-29 実機確定)。設定は JSON 1本で渡す。
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
    condition     = length(oci_generative_ai_hosted_application.agent) == 3 && length(oci_generative_ai_hosted_deployment.agent) == 3
    error_message = "All three SDK containers must be deployed by default (ADR-0019)."
  }

  # ゼロスケールが公開スタックの費用前提。既定で min_replica=0 が崩れていないこと。
  assert {
    condition = alltrue([
      for app in values(oci_generative_ai_hosted_application.agent) :
      app.scaling_config[0].min_replica == 0 && app.scaling_config[0].scaling_type == "CONCURRENCY"
    ])
    error_message = "Hosted agents must default to scale-to-zero (min_replica=0)."
  }

  # inbound の type は "IDCS" ではなく "IDCS_AUTH_CONFIG"("IDCS" は 400 になる — 実機確定)。
  # 突合するのは token の aud と**短い**スコープ名。
  assert {
    condition = alltrue([
      for app in values(oci_generative_ai_hosted_application.agent) : (
        app.inbound_auth_config[0].inbound_auth_config_type == "IDCS_AUTH_CONFIG" &&
        app.inbound_auth_config[0].idcs_config[0].audience == "jetuse-spike-ha01-agent" &&
        app.inbound_auth_config[0].idcs_config[0].scope == "invoke"
      )
    ])
    error_message = "Inbound auth must be IDCS_AUTH_CONFIG with the stack-scoped audience and short scope name."
  }

  # client_credentials の自給自足構成: client 兼 resource + 自分の fqs を allowed_scopes に持つ。
  assert {
    condition = (
      oci_identity_domains_app.agent.client_type == "confidential" &&
      oci_identity_domains_app.agent.is_oauth_client == true &&
      oci_identity_domains_app.agent.is_oauth_resource == true &&
      oci_identity_domains_app.agent.allowed_grants == toset(["client_credentials"]) &&
      contains([for s in oci_identity_domains_app.agent.allowed_scopes : s.fqs], "jetuse-spike-ha01-agentinvoke")
    )
    error_message = "The OAuth app must be a confidential client_credentials client that allows its own resource scope."
  }

  # 画像は <registry>/<prefix>-agent-<sdk>:<tag>。release.yml の push 先と一致していること。
  assert {
    condition     = oci_generative_ai_hosted_deployment.agent["langgraph"].active_artifact[0].container_uri == "ord.ocir.io/testnamespace/jetuse-agent-langgraph"
    error_message = "Container URI must match the repository names pushed by release.yml."
  }

  # 設定は JSON オブジェクト1本(JETUSE_AGENT_CONFIG)。スカラーを個別に渡す形へ戻すと、
  # コンテナには引用符付きの値が届いて壊れる。
  assert {
    condition = alltrue([
      for app in values(oci_generative_ai_hosted_application.agent) : (
        length(app.environment_variables) == 1 &&
        app.environment_variables[0].name == "JETUSE_AGENT_CONFIG" &&
        app.environment_variables[0].type == "PLAINTEXT" &&
        can(jsondecode(nonsensitive(app.environment_variables[0].value))) &&
        jsondecode(nonsensitive(app.environment_variables[0].value))["OCI_REGION"] == "us-chicago-1"
      )
    ])
    error_message = "Container settings must be passed as a single JSON object env var that round-trips unchanged."
  }

  # ACTIVE なデプロイメントは直接消せないため、Application 側のカスケード削除を担う
  # ガードリソースが SDK ごとに存在すること(destroy 失敗の回帰防止)。
  assert {
    condition     = length(terraform_data.cascade_delete) == 3
    error_message = "Each SDK needs a cascade-delete guard so destroy removes the application first."
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

  expect_failures = [oci_generative_ai_hosted_application.agent]
}
