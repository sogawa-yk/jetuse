data "oci_objectstorage_namespace" "this" {
  compartment_id = var.compartment_ocid
}

locals {
  ns = data.oci_objectstorage_namespace.this.namespace
}

# SPAビルド成果物（非公開。配信はAPI GW経由 + バケット読取PAR — ADR-0004）
resource "oci_objectstorage_bucket" "spa" {
  compartment_id = var.compartment_ocid
  namespace      = local.ns
  name           = "${var.prefix}-spa"
  access_type    = "NoPublicAccess"
}

# bucket_listing_actionは未指定=リスト不可。"Deny"を明示するとAPIが値を
# 返さず毎applyで再作成(URL変化)になるため指定しない
# 相対期限の基準時刻を state に固定する(time_offset)。timestamp() と違い base を保持するため
# plan 毎に揺れず、ignore_changes 無しで安定する(=明示指定時の後からの変更も反映できる)。
# 既存(固定日付)スタックでは初回 apply で PAR が 1回だけ相対期限へ再発行される(URL は API GW へ再配線)。
resource "time_offset" "spa_par" {
  count        = var.spa_par_expiry == "" ? 1 : 0
  offset_years = 1
}

resource "oci_objectstorage_preauthrequest" "spa_read" {
  namespace   = local.ns
  bucket      = oci_objectstorage_bucket.spa.name
  name        = "${var.prefix}-spa-read"
  access_type = "AnyObjectRead"
  # 空なら apply 時刻(time_offset の base)起点 +1年。明示指定時はその値を尊重(変更も反映)。
  time_expires = var.spa_par_expiry != "" ? var.spa_par_expiry : time_offset.spa_par[0].rfc3339
}

resource "oci_objectstorage_bucket" "app_data" {
  compartment_id = var.compartment_ocid
  namespace      = local.ns
  name           = "${var.prefix}-app-data"
  access_type    = "NoPublicAccess"
}

resource "oci_objectstorage_bucket" "speech" {
  compartment_id = var.compartment_ocid
  namespace      = local.ns
  name           = "${var.prefix}-speech"
  access_type    = "NoPublicAccess"
}

# --- Destroy 時のバケット空け(FIX-58) ---------------------------------------
# OCI provider に force_destroy 相当は無く、アプリが**実行時に書いたオブジェクト**
# (RAGアップロード・議事録音声・OCR中間物など)が残っていると destroy が
# `409-BucketNotEmpty` で必ず失敗する。Terraform が作ったオブジェクトしか消えないため、
# 「アプリを使ったユーザーはスタックを壊せない」状態になる(2026-07-28 実機で再現)。
# バケット削除の直前に bulk-delete でオブジェクトと版を消す。
# Resource Manager の実行環境には oci CLI が委任トークンで認証済みで同梱されている。
# for_each のキーは apply 前に確定する必要があるため、バケット resource の属性ではなく
# 同じ命名規則の静的な文字列を使い、順序は depends_on で担保する
# (destroy はこの resource → バケット の順に進む)。
resource "terraform_data" "empty_buckets" {
  for_each = toset(["${var.prefix}-spa", "${var.prefix}-app-data", "${var.prefix}-speech"])

  input = { namespace = local.ns, bucket = each.value, region = var.region }

  depends_on = [
    oci_objectstorage_bucket.spa,
    oci_objectstorage_bucket.app_data,
    oci_objectstorage_bucket.speech,
  ]

  provisioner "local-exec" {
    when       = destroy
    on_failure = continue # 空け損ねてもバケット削除本体のエラーを見せる
    environment = {
      JETUSE_OS_REGION = self.input.region
    }
    command = <<-CMD
      set -x
      NS="${self.input.namespace}"
      B="${self.input.bucket}"
      # provider と同じリージョンを CLI にも効かせる(既定任せだと別リージョンを掃除しうる)。
      # OCI CLI は OCI_CLI_REGION を尊重するので、引数の組み立ては不要。
      [ -n "$JETUSE_OS_REGION" ] && export OCI_CLI_REGION="$JETUSE_OS_REGION"
      # 失敗しても destroy 全体は止めない(on_failure=continue)が、黙って握り潰すと後段の
      # バケット削除が 409 になった理由が追えないため、必ず警告を残す。
      oci os object bulk-delete --force --namespace "$NS" --bucket-name "$B" \
        || echo "WARNING: bulk-delete failed for $B — バケット削除が 409-BucketNotEmpty になる可能性があります" >&2
      # 版付きバケット(将来versioningを有効にした場合)への保険
      oci os object bulk-delete-versions --force --namespace "$NS" --bucket-name "$B" \
        || echo "WARNING: bulk-delete-versions failed for $B" >&2
      # 未完了のマルチパートアップロードもバケット削除を妨げる。abort は --object-name と
      # --upload-id が必須なので、list して1件ずつ中断する(バケット名だけの abort は必ず失敗する)。
      # 抽出は CLI の --query + --raw-output だけで行う(外部の python に依存しない。
      # f-string 内のバックスラッシュは Python 3.11 以前で SyntaxError になるため使わない)。
      UPLOADS="$(oci os multipart list --namespace "$NS" --bucket-name "$B" --all \
        --query 'data[].join(`|`, [object, "upload-id"])' --raw-output 2>/dev/null)" \
        || echo "WARNING: multipart list failed for $B" >&2
      printf '%s\n' "$UPLOADS" | tr -d '[]",' | sed 's/^ *//; s/ *$//' \
        | while IFS='|' read -r obj uid; do
            [ -n "$uid" ] || continue
            oci os multipart abort --force --namespace "$NS" --bucket-name "$B" \
              --object-name "$obj" --upload-id "$uid" \
              || echo "WARNING: multipart abort failed for $B/$obj" >&2
          done
    CMD
  }
}
