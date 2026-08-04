# --- SP3-09: ORASEJAPAN 共有テナンシ鍵材料の Vault 化(施主指示 2026-07-09)。 ---
# 鍵材料を RM sensitive 変数(SP3-07)から jetuse:dev の Vault シークレットへ移す。
# 実値は tf に置かない — placeholder("{}")で作成し、ops/deploy-dev-app.sh seed-env が
# ローカル ~/.oci の ORASEJAPAN プロファイルから読んで新版を投入する(ignore_changes)。
# placeholder のままなら API 側(gen_shared_vault)は必須キー欠落で fail-closed(共有モデル 403)。
# 注意: KMS Vault は削除に猶予期間がある(乱造・作り直し禁止)。destroy しない。

resource "oci_kms_vault" "gen_shared" {
  compartment_id = var.compartment_ocid
  display_name   = "${local.prefix}-vault"
  vault_type     = "DEFAULT"

  # tasks/SP3-09「destroy 禁止」をコメントでなく計画で強制する(review-2 M002)。
  # 誤 destroy / 置換計画は通常 apply を弾く。真の破棄は人間ゲート付き break-glass
  # (この行を外す)でのみ。KMS Vault は削除に猶予期間があり即時復旧が難しいため。
  lifecycle {
    prevent_destroy = true
  }
}

# software 保護キー(HSM を使わない = 追加課金なし — tasks/SP3-09)
resource "oci_kms_key" "gen_shared" {
  compartment_id      = var.compartment_ocid
  display_name        = "${local.prefix}-gen-shared-key"
  management_endpoint = oci_kms_vault.gen_shared.management_endpoint
  protection_mode     = "SOFTWARE"

  key_shape {
    algorithm = "AES"
    length    = 32
  }

  lifecycle {
    prevent_destroy = true
  }
}

# 鍵材料一式を 1 つの JSON シークレットで持つ: {user, tenancy, fingerprint, region, key_pem}
resource "oci_vault_secret" "gen_shared" {
  compartment_id = var.compartment_ocid
  vault_id       = oci_kms_vault.gen_shared.id
  key_id         = oci_kms_key.gen_shared.id
  secret_name    = "${local.prefix}-gen-shared"

  secret_content {
    content_type = "BASE64"
    content      = base64encode("{}")
  }

  lifecycle {
    # 実値の新版投入は seed-env(CLI)。tf は器だけ管理し内容に関与しない
    ignore_changes = [secret_content]
    # destroy 禁止(review-2 M002)。破棄は人間ゲート付き break-glass のみ
    prevent_destroy = true
  }
}

output "gen_shared_secret_ocid" {
  value = oci_vault_secret.gen_shared.id
}
