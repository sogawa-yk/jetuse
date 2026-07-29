locals {
  adb_admin_password = var.adb_admin_password != "" ? var.adb_admin_password : random_password.adb_admin.result
  db_name            = substr(replace(var.prefix, "-", ""), 0, 14)

  # コンテナイメージ(ADR-0011): 明示指定が無ければ OCIR パスを合成。
  # repo は手動管理(genu-proto)。パスはネームスペースベースでコンパートメント非依存。
  # repo名は image_repo_prefix(既定 jetuse)で合成し、リソース名の var.prefix とは分離する。
  # → prefix を変えてもイメージ参照(release.yml が push する jetuse-*)が壊れない。
  # OCI Functions は「関数と同一リージョンの OCIR イメージ」しか受け付けない(ADR-0011)ため、
  # イメージは release.yml が対応4リージョン(大阪/東京/アシュバーン/シカゴ)の OCIR へ事前 push し、
  # レジストリはデプロイリージョンの OCIR を自動選択する(Issue #55 / ADR-0017)。
  # 対応外リージョンは main.tf の region_guard が plan 時に明示エラーにする
  # (api_image_url と fn_router_image の両方を明示指定すれば対応外リージョンでも可)。
  ocir_supported_region_keys = ["kix", "nrt", "iad", "ord"]
  # GenAI(推論+agentic API)の実証済みリージョンは OCIR より狭い(docs/tips.md)。
  # kix(大阪)/ord(シカゴ)のみ。nrt/iad は apply は通るが GenAI が動かない。
  genai_validated_region_keys = ["kix", "ord"]
  # JetUse 公開イメージの namespace。auto-synth(空 image URL 時)が pull 可能なパスを作れるのは
  # この公開 namespace のときだけ。var.ocir_namespace の既定値と一致させること(region_guard が検証)。
  public_ocir_namespace = "idqcucnenh88"
  deploy_region_key = try(lower(one([
    for r in data.oci_identity_region_subscriptions.this.region_subscriptions : r.region_key
    if r.region_name == var.region
  ])), "")

  # テナンシのホームリージョン(Identity Domain 作成先)。providers.tf の home alias と同式。
  home_region = try([for r in data.oci_identity_region_subscriptions.this.region_subscriptions :
  r.region_name if r.is_home_region][0], var.region)
  ocir_registry   = "${local.deploy_region_key}.ocir.io/${var.ocir_namespace}"
  api_image_url   = var.api_image_url != "" ? var.api_image_url : "${local.ocir_registry}/${var.image_repo_prefix}-api:${var.image_tag}"
  fn_router_image = var.fn_router_image != "" ? var.fn_router_image : "${local.ocir_registry}/${var.image_repo_prefix}-fn-router:${var.image_tag}"

  # OIDC: enable_auth=false の間は空(SPAはdev-userモード)
  domain_url     = var.enable_auth ? module.identity_domain[0].domain_url : ""
  oidc_client_id = var.enable_auth ? module.identity_domain_app[0].client_id : ""

  # ホスト型エージェント(PORT-03 / ADR-0019)を配備するか。3条件すべてが要る:
  #  1. 利用者が無効化していない
  #  2. enable_auth=true — OAuth(client_credentials)の発行元 Identity Domain が要る。
  #     enable_auth=false のスタックには発行元が無く、この構成自体が成立しない。
  #  3. デプロイリージョンがエージェント画像の push 先であること。画像は GenAI 実証済みの
  #     kix/ord にしか push しない(release.yml)。nrt/iad では Deployment の image pull が
  #     必ず失敗するうえ、仮に動いてもコンテナ自身の GenAI 呼び出しが通らない。
  #     ここは allow_unvalidated_genai_region ではオプトインさせない(画像が存在しないため)。
  hosted_agents_enabled = (
    var.enable_hosted_agents
    && var.enable_auth
    && contains(local.genai_validated_region_keys, local.deploy_region_key)
  )
  agent_app_ocids = local.hosted_agents_enabled ? module.hosted_agent[0].app_ocids : {}
  # 明示指定が無ければ API/Functions と同じくデプロイリージョンの OCIR を合成する。
  # 公開既定以外の namespace では main.tf の region_guard が明示指定を必須にする。
  agent_image_registry = var.hosted_agent_image_registry != "" ? var.hosted_agent_image_registry : local.ocir_registry

  # API コンテナとエージェントコンテナの両方が読む素材。
  # api_environment 経由でエージェントへ渡すと
  # api_environment -> module.hosted_agent -> api_environment の循環参照になるため、
  # 共有分だけをここに切り出して両者が参照する。
  shared_runtime_environment = {
    OCI_REGION       = var.region
    COMPARTMENT_OCID = var.compartment_ocid
    # 空ならアプリが自動解決(FIX-47)。genai.py resolve_project_ocid 参照。
    PROJECT_OCID       = var.project_ocid
    AUTH_MODE          = "resource_principal"
    ADB_QUERY_PASSWORD = random_password.jetuse_query.result
    ADB_DSN            = "${local.db_name}_low"
    # NL2SQL(SQL Search)。事前作成した Semantic Store の OCID(空なら NL2SQL 無効=503)。
    SEMSTORE_OCID       = var.semstore_ocid
    ADB_WALLET_PASSWORD = random_password.wallet.result
    # ウォレットは Terraform が base64テキストでバケットへ配置(コンテナはobject readで取得・デコード)
    ADB_WALLET_BUCKET = module.object_storage.app_data_bucket
    ADB_WALLET_OBJECT = "adb_wallet.zip.b64"
    ADB_WALLET_BASE64 = "true"
  }

  # Container Instance / Functions に渡す環境変数(jetuse_core.settings のフィールド名に対応)。
  # CIは OIDC issuer/JWKS のみ参照し client_id には依存しない(循環回避)。
  api_environment = merge(local.shared_runtime_environment, {
    PROJECT_AUTOCREATE = var.enable_project_autocreate ? "true" : "false"
    AUTH_REQUIRED      = var.enable_auth ? "true" : "false"
    OIDC_ISSUER        = var.enable_auth ? "https://identity.oraclecloud.com/" : ""
    OIDC_JWKS_URL      = var.enable_auth ? "${local.domain_url}/admin/v1/SigningCert/jwk" : ""
    # Select AI は ADB のリソースプリンシパル資格情報を使う(bootstrapがENABLE_RESOURCE_PRINCIPAL)
    SELECT_AI_CREDENTIAL = "OCI$RESOURCE_PRINCIPAL"
    # DB自己ブートストラップ(entrypoint.sh → jetuse_core.bootstrap)
    RUN_DB_BOOTSTRAP   = "true"
    ADB_ADMIN_PASSWORD = local.adb_admin_password
    ADB_USER           = "JETUSE_APP"
    ADB_QUERY_USER     = "JETUSE_QUERY"
    ADB_PASSWORD       = random_password.jetuse_app.result
    ADB_OCID           = module.adb.adb_id # フォールバック(バケット未配置時にAPI生成)
    RAG_BUCKET         = module.object_storage.app_data_bucket
    SPEECH_BUCKET      = module.object_storage.speech_bucket
    OS_NAMESPACE       = module.object_storage.namespace
    # Monitoring 名前空間は prefix 由来にする(既定 "jetuse_dev" のままだと別テナンシに
    # dev 名前空間が出る)。名前空間はハイフン不可なので "_" へ正規化。
    METRICS_NAMESPACE = replace(var.prefix, "-", "_")
    # 管理ダッシュボード(/admin)の閲覧者。空のままだと is_admin が常に false になり、
    # ワンクリック配備では誰も /api/admin/usage を開けない(403)。認証有効時は
    # スタックが作る唯一のログインユーザーを既定の管理者にする。
    # JWT の subject は Identity Domain のユーザー名なので "demo" を渡す(email claim は空)。
    # 空白だけの入力で「管理者0人」に落ちないよう trimspace して判定・送出する。
    ADMIN_USERS = trimspace(var.admin_users) != "" ? trimspace(var.admin_users) : (var.enable_auth ? "demo" : "")
  })

  # ホスト型エージェントのコンテナへ渡す環境変数。API コンテナ用の設定(OIDC / bootstrap /
  # 管理者 / 書き込み系バケット)は不要なので共有分だけを渡す(最小権限・最小設定)。
  agent_environment = local.shared_runtime_environment

  # エージェント invoke 用の OAuth 資格情報と Application OCID。
  # **API の Container Instance にだけ**渡す。Functions ルーターが担当するのは
  # presets / dbchat / tts でエージェントを呼ばないため、同じ map に載せると
  # client_secret を必要のない実行体へ配ることになる(review F-006)。
  # 未配備なら空のままで、アプリは理由付きで縮退する(jetuse_core.hosted_agent.availability)。
  hosted_agent_environment = {
    HOSTED_AGENT_IDCS_DOMAIN   = local.hosted_agents_enabled ? local.domain_url : ""
    HOSTED_AGENT_CLIENT_ID     = local.hosted_agents_enabled ? module.hosted_agent[0].client_id : ""
    HOSTED_AGENT_CLIENT_SECRET = local.hosted_agents_enabled ? module.hosted_agent[0].client_secret : ""
    HOSTED_AGENT_SCOPE         = local.hosted_agents_enabled ? module.hosted_agent[0].scope : ""
    # 「配備する構成か」をアプリへ伝える。未配備を故障扱いして /api/health 全体を
    # 赤くしないための区別に使う(review F-007)。
    HOSTED_AGENTS_ENABLED    = local.hosted_agents_enabled ? "true" : "false"
    AGENT_OPENAI_APP_OCID    = lookup(local.agent_app_ocids, "openai", "")
    AGENT_LANGGRAPH_APP_OCID = lookup(local.agent_app_ocids, "langgraph", "")
    AGENT_ADK_APP_OCID       = lookup(local.agent_app_ocids, "adk", "")
  }
}
