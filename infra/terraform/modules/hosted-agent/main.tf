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

# --- Hosted Application / Deployment は OCI CLI 経由で作る（provider を使えない） ---
#
# oracle/oci 8.24.0（2026-07-29 時点の最新）の `oci_generative_ai_hosted_application` は
# **必ず失敗する**。リソース作成・削除そのものは成功し work request も SUCCEEDED になるが、
# provider の待ち受け(`hostedApplicationWaitForWorkRequest`)が
#   strings.Contains(strings.ToLower(res.EntityType), "hostedapplication")
# で照合する一方、サービスが返す entityType は **"HOSTED_APPLICATION"**（アンダースコア入り）で、
# 小文字化しても "hosted_application" にしかならず一致しない。結果 identifier が nil のまま
# 「work request did not succeed」と誤判定され、リソースは tainted になって次の apply で
# 削除→再作成→また誤判定、と収束しない（2026-07-29 DEPLOYTEST/us-chicago-1 実機確認）。
#
# よって作成・削除は CLI(`oci raw-request`)で行い、**参照はデータソース**で行う
# （データソースは list/get だけで work request を待たないため影響を受けない）。
# 判定ロジックのバグは create/update/delete の待ち受けに限られる。
#
# CLI は `raw-request` に固定する: Resource Manager 同梱の OCI CLI には
# `generative-ai hosted-application` サブコマンドが無い版があり得るため専用サブコマンドに依存しない。
# また `raw-request` は `--query` を無視する（2026-07-29 実測）ので、抽出は grep で行う。
resource "terraform_data" "agent" {
  for_each = var.sdks

  # destroy 時の provisioner は self しか参照できないため、必要な値を input に持たせる。
  input = {
    region       = var.region
    compartment  = var.compartment_ocid
    display_name = "${var.prefix}-agent-${each.key}"
  }

  # 設定が変わったら作り直す（provider 管理ではないので、差分検出はこの指紋が担う）。
  triggers_replace = [sha256(jsonencode({
    display_name  = "${var.prefix}-agent-${each.key}"
    compartment   = var.compartment_ocid
    region        = var.region
    idcs_endpoint = var.idcs_endpoint
    audience      = local.audience
    scope         = local.scope_name
    min_replica   = var.min_replica
    max_replica   = var.max_replica
    concurrency   = var.target_concurrency_threshold
    container_uri = "${var.image_registry}/${var.image_repo_prefix}-agent-${each.key}"
    tag           = var.image_tag
    env           = var.environment_variables
  }))]

  provisioner "local-exec" {
    # 設定 JSON は環境変数で渡す（コマンド行に秘密を置かない）。
    environment = {
      JETUSE_AGENT_CONFIG = jsonencode(var.environment_variables)
    }
    command = <<-CMD
      set -eu
      BASE="https://generativeai.${var.region}.oci.oraclecloud.com/20231130"
      COMP="${var.compartment_ocid}"
      NAME="${var.prefix}-agent-${each.key}"
      TMP="$(mktemp -d)"
      trap 'rm -rf "$TMP"' EXIT
      api() { oci raw-request --region "${var.region}" "$@"; }
      # OCID / 状態の取り出しは grep で行う（raw-request は --query を無視する）。
      pick_ocid() { grep -oE '"id": "ocid1\.'"$1"'[^"]*"' | head -1 | sed -E 's/.*"(ocid1[^"]*)"/\1/'; }
      pick_state() { grep -oE '"lifecycleState": "[A-Z_]+"' | head -1 | sed -E 's/.*"([A-Z_]+)"/\1/'; }

      # 1) 既存の ACTIVE なアプリがあれば再利用する（再実行を安全にする）。
      #    削除済みも同名で列挙されるため lifecycleState=ACTIVE で絞る。
      APP="$(api --http-method GET --target-uri "$BASE/hostedApplications?compartmentId=$COMP&displayName=$NAME&lifecycleState=ACTIVE" 2>/dev/null | pick_ocid generativeaihostedapplication || true)"

      if [ -z "$APP" ]; then
        # env の JSON を JSON 文字列として埋め込むためエスケープする（python 等に依存しない）。
        ESCAPED="$(printf '%s' "$JETUSE_AGENT_CONFIG" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')"
        cat > "$TMP/app.json" <<JSON
      {"displayName":"$NAME","compartmentId":"$COMP",
       "description":"JetUse ReAct agent container (${each.key} SDK)",
       "scalingConfig":{"scalingType":"CONCURRENCY","minReplica":${var.min_replica},"maxReplica":${var.max_replica},"targetConcurrencyThreshold":${var.target_concurrency_threshold}},
       "inboundAuthConfig":{"inboundAuthConfigType":"IDCS_AUTH_CONFIG","idcsConfig":{"domainUrl":"${var.idcs_endpoint}","audience":"${local.audience}","scope":"${local.scope_name}"}},
       "environmentVariables":[{"name":"JETUSE_AGENT_CONFIG","type":"PLAINTEXT","value":"$ESCAPED"}]}
      JSON
        APP="$(api --http-method POST --target-uri "$BASE/hostedApplications" --request-body "file://$TMP/app.json" | pick_ocid generativeaihostedapplication)"
        [ -n "$APP" ] || { echo "failed to create hosted application $NAME" >&2; exit 1; }
        i=0
        while [ "$i" -lt 60 ]; do
          i=$((i + 1))
          ST="$(api --http-method GET --target-uri "$BASE/hostedApplications/$APP" | pick_state)"
          [ "$ST" = ACTIVE ] && break
          case "$ST" in FAILED|DELETED)
            echo "hosted application $NAME entered $ST (inbound_auth_config の domain URL が実在するか確認してください)" >&2; exit 1 ;;
          esac
          sleep 10
        done
        [ "$ST" = ACTIVE ] || { echo "hosted application $NAME did not become ACTIVE" >&2; exit 1; }
      fi

      # 2) デプロイメント（イメージ pull + 脆弱性スキャンを伴うため時間がかかる）。
      #    1アプリ=1デプロイメントなので、既にあれば作らない。
      DEP="$(api --http-method GET --target-uri "$BASE/hostedDeployments?compartmentId=$COMP&applicationId=$APP" 2>/dev/null | pick_ocid generativeaihosteddeployment || true)"
      if [ -z "$DEP" ]; then
        cat > "$TMP/dep.json" <<JSON
      {"displayName":"$NAME-dep","compartmentId":"$COMP","hostedApplicationId":"$APP",
       "activeArtifact":{"artifactType":"SIMPLE_DOCKER_ARTIFACT","containerUri":"${var.image_registry}/${var.image_repo_prefix}-agent-${each.key}","tag":"${var.image_tag}"}}
      JSON
        DEP="$(api --http-method POST --target-uri "$BASE/hostedDeployments" --request-body "file://$TMP/dep.json" | pick_ocid generativeaihosteddeployment)"
        [ -n "$DEP" ] || { echo "failed to create hosted deployment for $NAME" >&2; exit 1; }
      fi
      i=0
      while [ "$i" -lt 120 ]; do
        i=$((i + 1))
        BODY="$(api --http-method GET --target-uri "$BASE/hostedDeployments/$DEP")"
        ST="$(printf '%s' "$BODY" | pick_state)"
        [ "$ST" = ACTIVE ] && break
        case "$ST" in FAILED|NEEDS_ATTENTION|DELETED)
          echo "hosted deployment for $NAME entered $ST" >&2; exit 1 ;;
        esac
        sleep 15
      done
      [ "$ST" = ACTIVE ] || { echo "hosted deployment for $NAME did not become ACTIVE within 30 minutes" >&2; exit 1; }
      echo "hosted agent ready: $NAME ($APP)"
    CMD
  }

  lifecycle {
    precondition {
      condition     = var.min_replica <= var.max_replica
      error_message = "hosted-agent: min_replica (${var.min_replica}) must not exceed max_replica (${var.max_replica})."
    }
  }

  # destroy: ACTIVE なデプロイメントは直接削除できず、アプリ削除でカスケードされる
  # (ops/recreate-agents.sh の実機記録)。self しか参照できないので名前で引き直す。
  provisioner "local-exec" {
    when    = destroy
    command = <<-CMD
      set -eu
      BASE="https://generativeai.${self.input.region}.oci.oraclecloud.com/20231130"
      api() { oci raw-request --region "${self.input.region}" "$@"; }
      APP="$(api --http-method GET --target-uri "$BASE/hostedApplications?compartmentId=${self.input.compartment}&displayName=${self.input.display_name}&lifecycleState=ACTIVE" 2>/dev/null | grep -oE '"id": "ocid1\.generativeaihostedapplication[^"]*"' | head -1 | sed -E 's/.*"(ocid1[^"]*)"/\1/' || true)"
      [ -n "$APP" ] || { echo "hosted application ${self.input.display_name} は既に存在しません"; exit 0; }
      api --http-method DELETE --target-uri "$BASE/hostedApplications/$APP" >/dev/null
      # DELETED でも GET は 200 を返す（404 とは限らない）ので lifecycleState で判定する。
      i=0
      while [ "$i" -lt 60 ]; do
        i=$((i + 1))
        ST="$(api --http-method GET --target-uri "$BASE/hostedApplications/$APP" 2>/dev/null | grep -oE '"lifecycleState": "[A-Z_]+"' | head -1 | sed -E 's/.*"([A-Z_]+)"/\1/' || echo GONE)"
        case "$ST" in DELETED|GONE) exit 0 ;; esac
        sleep 12
      done
      echo "hosted application ${self.input.display_name} did not reach DELETED" >&2
      exit 1
    CMD
  }
}

# 作成済みリソースの OCID は**データソース**で取る（work request を待たないので provider バグの影響外）。
data "oci_generative_ai_hosted_applications" "agent" {
  for_each = var.sdks

  compartment_id = var.compartment_ocid
  display_name   = "${var.prefix}-agent-${each.key}"
  state          = "ACTIVE"

  depends_on = [terraform_data.agent]
}
