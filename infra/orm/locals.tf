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
  api_image_url   = var.api_image_url != "" ? var.api_image_url : "${local.ocir_registry}/${var.image_repo_prefix}-api:latest"
  fn_router_image = var.fn_router_image != "" ? var.fn_router_image : "${local.ocir_registry}/${var.image_repo_prefix}-fn-router:latest"

  # OIDC: enable_auth=false の間は空(SPAはdev-userモード)
  domain_url     = var.enable_auth ? module.identity_domain[0].domain_url : ""
  oidc_client_id = var.enable_auth ? module.identity_domain_app[0].client_id : ""

  # Container Instance / Functions に渡す環境変数(jetuse_core.settings のフィールド名に対応)。
  # CIは OIDC issuer/JWKS のみ参照し client_id には依存しない(循環回避)。
  api_environment = {
    OCI_REGION       = var.region
    COMPARTMENT_OCID = var.compartment_ocid
    # 空ならアプリが自動解決(FIX-47)。genai.py resolve_project_ocid 参照。
    PROJECT_OCID       = var.project_ocid
    PROJECT_AUTOCREATE = var.enable_project_autocreate ? "true" : "false"
    AUTH_MODE          = "resource_principal"
    AUTH_REQUIRED      = var.enable_auth ? "true" : "false"
    # SP2-04: issuer/JWKS は上書き可(世代差分吸収 — specs/18 §5.1)。3点の非空は oidc_config_guard で plan 時強制
    OIDC_ISSUER   = var.enable_auth ? var.oidc_issuer : ""
    OIDC_JWKS_URL = var.enable_auth ? (trimspace(var.oidc_jwks_url) != "" ? trimspace(var.oidc_jwks_url) : "${local.domain_url}/admin/v1/SigningCert/jwk") : ""
    OIDC_AUDIENCE = var.enable_auth ? var.oidc_audience : ""
    # Select AI は ADB のリソースプリンシパル資格情報を使う(bootstrapがENABLE_RESOURCE_PRINCIPAL)
    SELECT_AI_CREDENTIAL = "OCI$RESOURCE_PRINCIPAL"
    # DB自己ブートストラップ(entrypoint.sh → jetuse_core.bootstrap)
    RUN_DB_BOOTSTRAP    = "true"
    ADB_ADMIN_PASSWORD  = local.adb_admin_password
    ADB_USER            = "JETUSE_APP"
    ADB_QUERY_USER      = "JETUSE_QUERY"
    ADB_PASSWORD        = random_password.jetuse_app.result
    ADB_QUERY_PASSWORD  = random_password.jetuse_query.result
    ADB_DSN             = "${local.db_name}_low"
    ADB_WALLET_PASSWORD = random_password.wallet.result
    # ウォレットは Terraform が base64テキストでバケットへ配置(コンテナはobject readで取得・デコード)
    ADB_WALLET_BUCKET = module.object_storage.app_data_bucket
    ADB_WALLET_OBJECT = "adb_wallet.zip.b64"
    ADB_WALLET_BASE64 = "true"
    ADB_OCID          = module.adb.adb_id # フォールバック(バケット未配置時にAPI生成)
    RAG_BUCKET        = module.object_storage.app_data_bucket
    SPEECH_BUCKET     = module.object_storage.speech_bucket
    OS_NAMESPACE      = module.object_storage.namespace
    # Monitoring 名前空間は prefix 由来にする(既定 "jetuse_dev" のままだと別テナンシに
    # dev 名前空間が出る)。名前空間はハイフン不可なので "_" へ正規化。
    METRICS_NAMESPACE = replace(var.prefix, "-", "_")
    # NL2SQL(SQL Search)。事前作成した Semantic Store の OCID(空なら NL2SQL 無効=503)。
    SEMSTORE_OCID = var.semstore_ocid
  }
}
