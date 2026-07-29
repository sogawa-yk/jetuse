# PORT-03 / ADR-0019: 公開スタックからホスト型エージェント(Enterprise AI Agent)を配備する。
#
# AGT-04(docs/setup/hosted-agent-oauth.md)では OAuth アプリの作成も Hosted Application の
# 配備も手作業だった。oracle/oci 8.x に必要な resource が揃ったため、ここで宣言的に組む。
#
# OAuth は「自給自足」構成: 1つの IDCS アプリを **client 兼 resource** にし、
#   - client として client_credentials で token を取り(scope = 完全修飾スコープ fqs)
#   - resource として Hosted Application の inbound 検証(aud / scope 突合)に使う
# ことで、アプリが自分で発行した token で自分が守る invoke を呼べる(AGT-04 実証構成)。

locals {
  # audience は Identity Domain 内で一意でなければならないため prefix から作る。
  audience   = "${var.prefix}-agent"
  scope_name = "invoke"
  # 完全修飾スコープ(fqs)は audience と scope の単純連結(セパレータ無し)。
  # 2026-06-12 実機確認済み(docs/tips.md)。自分の scopes ブロックの computed fqs は
  # 同一リソース内から参照できないため、allowed_scopes にはこの規則で組み立てた値を渡す。
  fqs = "${local.audience}${local.scope_name}"
}

# --- OAuth クライアント兼リソース ---
resource "oci_identity_domains_app" "agent" {
  idcs_endpoint = var.idcs_endpoint
  schemas       = ["urn:ietf:params:scim:schemas:oracle:idcs:App"]
  display_name  = "${var.prefix}-agent"
  description   = "JetUse hosted agent invocation (client_credentials, self-audience)"

  based_on_template {
    value = "CustomWebAppTemplateId"
  }

  # client 側: confidential(client_secret を持つ)で client_credentials のみ許可する。
  is_oauth_client = true
  client_type     = "confidential"
  allowed_grants  = ["client_credentials"]

  # resource 側: この audience 宛の token を発行し、Hosted Application が同じ値で検証する。
  is_oauth_resource = true
  audience          = local.audience
  scopes {
    value            = local.scope_name
    description      = "Invoke JetUse hosted agent containers"
    requires_consent = false
  }

  # 自給自足構成の要: client として**自分が定義した scope** を要求できるようにする。
  # これが無いと client_credentials の token 要求が scope 不許可で失敗する
  # (is_oauth_client + is_oauth_resource + allowed_scopes の3点セット — docs/tips.md 2026-06-12)。
  allowed_scopes {
    fqs = local.fqs
  }

  active          = true
  is_login_target = false
  show_in_my_apps = false

  # destroy 前に非アクティブ化(active なアプリは削除できず destroy が 400 で失敗する)。
  # `app patch` に --force は無く、複合型の置換で y/N を尋ねるので y を流し込む(FIX-58 実機確定)。
  provisioner "local-exec" {
    when    = destroy
    command = <<-CMD
      echo y | oci identity-domains app patch \
        --endpoint "${self.idcs_endpoint}" \
        --app-id ${self.id} \
        --schemas '["urn:ietf:params:scim:api:messages:2.0:PatchOp"]' \
        --operations '[{"op": "replace", "path": "active", "value": false}]'
    CMD
  }
}

# --- SDK ごとのホスト型アプリケーション ---
resource "oci_generative_ai_hosted_application" "agent" {
  for_each = var.sdks

  compartment_id = var.compartment_ocid
  display_name   = "${var.prefix}-agent-${each.key}"
  description    = "JetUse ReAct agent container (${each.key} SDK)"

  # inbound_auth_config は必須。type は "IDCS" ではなく "IDCS_AUTH_CONFIG"
  # ("IDCS" は 400 Invalid InboundAuthConfigType — 2026-07-29 実機確定)。
  # ここで突合するのは token の aud と **短いスコープ名**(fqs ではない)。
  inbound_auth_config {
    inbound_auth_config_type = "IDCS_AUTH_CONFIG"
    idcs_config {
      domain_url = var.idcs_endpoint
      audience   = local.audience
      scope      = local.scope_name
    }
  }

  # min_replica=0 でアイドル時のレプリカが 0 になり、使わない利用者に継続課金が発生しない
  # (ACTIVE 到達後も 0 が保持されることを 2026-07-29 に実機確認 / ADR-0019)。
  scaling_config {
    scaling_type                 = "CONCURRENCY"
    min_replica                  = var.min_replica
    max_replica                  = var.max_replica
    target_concurrency_threshold = var.target_concurrency_threshold
  }

  # 設定は環境変数を1本にまとめた JSON で渡し、コンテナ側(agent_env.py)が展開する。
  #
  # なぜ1変数ずつ渡さないか(2026-07-29 実機確定):
  #   provider は environment_variables.value を「JSON として妥当な文字列」しか受け付けない
  #   (ValidateFunc=StringIsJSON)が、**アンマーシャルせずそのまま** API へ送る。API も文字列を
  #   verbatim に保存する。よって jsonencode("us-chicago-1") を渡すと、コンテナには
  #   引用符ごと `"us-chicago-1"` が届いて壊れる。JSON **オブジェクト**なら
  #   `{"OCI_REGION":"us-chicago-1"}` がそのまま往復することを実機で確認した。
  # 副次効果として、sensitive な map を for_each に使えない制約(plan が停止する)も回避できる。
  environment_variables {
    name  = "JETUSE_AGENT_CONFIG"
    type  = "PLAINTEXT"
    value = jsonencode(var.environment_variables)
  }

  timeouts {
    create = "30m"
    update = "30m"
    delete = "30m"
  }

  lifecycle {
    precondition {
      condition     = var.min_replica <= var.max_replica
      error_message = "hosted-agent: min_replica (${var.min_replica}) must not exceed max_replica (${var.max_replica})."
    }
  }
}

# --- コンテナの配備(イメージ pull + 脆弱性スキャンを伴うため時間がかかる) ---
resource "oci_generative_ai_hosted_deployment" "agent" {
  for_each = var.sdks

  compartment_id        = var.compartment_ocid
  display_name          = "${var.prefix}-agent-${each.key}-dep"
  hosted_application_id = oci_generative_ai_hosted_application.agent[each.key].id

  # 参照先は JetUse 公開ネームスペースの public OCIR リポジトリ。pull 自体は cross-tenancy で
  # 行えるが、Dynamic Group 側には公式要件どおり read repos / read vss-family が要る
  # (modules/iam の hosted_agent_statements — docs/setup/iam.md)。
  active_artifact {
    artifact_type = "SIMPLE_DOCKER_ARTIFACT"
    container_uri = "${var.image_registry}/${var.image_repo_prefix}-agent-${each.key}"
    tag           = var.image_tag
  }

  timeouts {
    create = "60m"
    update = "60m"
    delete = "60m"
  }
}

# --- destroy 順序の是正 ---
# **ACTIVE な Hosted Deployment は直接削除できない**。削除できるのは Application で、
# そのときデプロイメントがカスケード削除される(ops/recreate-agents.sh に実機確定の記録)。
# ところが Terraform の既定の destroy 順は「依存する側から」なので、Deployment →
# Application の順に消そうとして最初の1手で失敗する。
#
# この terraform_data は Deployment を参照する = Deployment より**先に**destroy される。
# ここで Application を削除して両方をまとめて消し、後続の provider による delete は
# 「既に無い」状態で通す。
#
# CLI は `oci raw-request`(汎用・古くから存在)を使う。Resource Manager 実行環境に同梱される
# OCI CLI には `generative-ai hosted-application` サブコマンドが無い版があり得るため、
# 専用サブコマンドには依存しない(委任トークンでの認証は raw-request でも効く)。
resource "terraform_data" "cascade_delete" {
  for_each = var.sdks

  # destroy 時の provisioner は self しか参照できないので、必要な値を input に持たせる。
  input = {
    application_uri = "https://generativeai.${var.region}.oci.oraclecloud.com/20231130/hostedApplications/${oci_generative_ai_hosted_application.agent[each.key].id}"
    sdk             = each.key
  }

  # Deployment を参照して destroy 順序(この resource が先)を作る。
  triggers_replace = [oci_generative_ai_hosted_deployment.agent[each.key].id]

  provisioner "local-exec" {
    when    = destroy
    command = <<-CMD
      set -u
      oci raw-request --http-method DELETE --target-uri "${self.input.application_uri}" >/dev/null 2>&1 || true
      # DELETED は GET が 200 を返したまま lifecycleState で示される(404 とは限らない)。
      # 両方を終了条件にする。
      i=0
      while [ "$i" -lt 60 ]; do
        i=$((i + 1))
        if ! body="$(oci raw-request --http-method GET --target-uri "${self.input.application_uri}" 2>/dev/null)"; then
          exit 0   # GET 自体が落ちる = 消えている
        fi
        case "$body" in *'"lifecycleState": "DELETED"'*|*'"lifecycleState":"DELETED"'*) exit 0 ;; esac
        sleep 12
      done
      echo "hosted application (${self.input.sdk}) did not reach DELETED within 12 minutes" >&2
      exit 1
    CMD
  }
}
