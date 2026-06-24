resource "oci_apigateway_gateway" "this" {
  compartment_id             = var.compartment_ocid
  display_name               = "${var.prefix}-apigw"
  endpoint_type              = "PUBLIC"
  subnet_id                  = var.subnet_id
  network_security_group_ids = [var.nsg_id]
}

locals {
  os_host = "https://objectstorage.${var.region}.oraclecloud.com"

  # ① CI(FastAPI)行き。SSEは readTimeoutInSeconds=300 明示必須(ADR-0003)。
  # 会話CRUD(CHAT-02 案A)もCI同居のためここに含める
  chat_routes = var.ci_base_url == "" ? [] : [
    {
      path         = "/api/chat/{p*}"
      methods      = ["ANY"]
      type         = "HTTP_BACKEND"
      url          = "${var.ci_base_url}/api/chat/$${request.path[p]}"
      function_id  = null
      read_timeout = 300
    },
    # OCR(ENH-07): 多ページPDFは分割×並列OCRで数十秒かかりうるため read_timeout を延長。
    # 同期ブロッキングのため60秒では504になる(ENH-07で実機確認)。完全一致ルート必須
    # ({p*}は末尾セグメントなしに一致しないため/api/ocr自体を拾えない)
    {
      path         = "/api/ocr"
      methods      = ["ANY"]
      type         = "HTTP_BACKEND"
      url          = "${var.ci_base_url}/api/ocr"
      function_id  = null
      read_timeout = 300
    },
    # 非ストリーミングAPIのキャッチオール(会話CRUD・プリセット等)。
    # /api/chat/{p*} の方が具体的なため優先される(実機確認済み)
    {
      path         = "/api/{p*}"
      methods      = ["ANY"]
      type         = "HTTP_BACKEND"
      url          = "${var.ci_base_url}/api/$${request.path[p]}"
      function_id  = null
      read_timeout = 60
    },
  ]

  # ② 非ストリーミング: Functionsバックエンド(ADR-0005)
  # {p*}は「/api/presets」のような末尾セグメントなしに一致しない(ARCH-02実測)ため
  # 完全一致ルートも併設する
  fn_routes = concat(
    [for k, fid in var.functions_routes : {
      path         = "/api/${k}"
      methods      = ["ANY"]
      type         = "ORACLE_FUNCTIONS_BACKEND"
      url          = null
      function_id  = fid
      read_timeout = null
    }],
    [for k, fid in var.functions_routes : {
      path         = "/api/${k}/{p*}"
      methods      = ["ANY"]
      type         = "ORACLE_FUNCTIONS_BACKEND"
      url          = null
      function_id  = fid
      read_timeout = null
    }],
  )

  # ③ 静的配信: Object Storage(非公開バケット+読取PAR)へのHTTPバックエンド(ADR-0004)
  spa_routes = var.spa_par_access_uri == "" ? [] : [
    {
      path         = "/"
      methods      = ["GET"]
      type         = "HTTP_BACKEND"
      url          = "${local.os_host}${var.spa_par_access_uri}index.html"
      function_id  = null
      read_timeout = null
    },
    {
      path         = "/{object*}"
      methods      = ["GET"]
      type         = "HTTP_BACKEND"
      url          = "${local.os_host}${var.spa_par_access_uri}$${request.path[object]}"
      function_id  = null
      read_timeout = null
    },
  ]

  routes = concat(local.chat_routes, local.fn_routes, local.spa_routes)
}

resource "oci_apigateway_deployment" "this" {
  compartment_id = var.compartment_ocid
  gateway_id     = oci_apigateway_gateway.this.id
  display_name   = "${var.prefix}-dep"
  path_prefix    = "/"

  specification {
    # SEC-03: 全体レート制限(送信元IP単位)。rate=0で無効
    dynamic "request_policies" {
      for_each = var.rate_limit_rps > 0 ? [1] : []
      content {
        rate_limiting {
          rate_in_requests_per_second = var.rate_limit_rps
          rate_key                    = var.rate_limit_key
        }
      }
    }

    dynamic "routes" {
      for_each = local.routes
      content {
        path    = routes.value.path
        methods = routes.value.methods
        backend {
          type                    = routes.value.type
          url                     = routes.value.url
          function_id             = routes.value.function_id
          read_timeout_in_seconds = routes.value.read_timeout
          # HTTP_BACKEND は connect/send timeout が >=1 必須(OCI制約)。
          # ルート挿入で状態インデックスがFunctionsルート(0)とずれた際に
          # 0のまま検証され400になるため、HTTP_BACKENDでは明示する
          connect_timeout_in_seconds = routes.value.type == "HTTP_BACKEND" ? 60 : null
          send_timeout_in_seconds    = routes.value.type == "HTTP_BACKEND" ? 300 : null
        }
      }
    }
  }
}
