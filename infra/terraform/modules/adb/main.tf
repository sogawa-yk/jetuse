# スパイクADB(jetuse-spike-adb)と同条件: ECPU・自動スケールなし・最小構成
resource "oci_database_autonomous_database" "this" {
  compartment_id = var.compartment_ocid
  display_name   = "${var.prefix}-adb"
  db_name        = substr(replace(var.prefix, "-", ""), 0, 14)
  db_workload    = var.db_workload
  # 26ai: Select AIベクトル索引(RAG-03)に必須(19cはORA-20047 — SPIKE-08)。
  # 19cからのアップグレード先は26ai(スケジュールアップグレードAPI経由 — tips参照)
  db_version                  = var.db_version
  compute_model               = "ECPU"
  compute_count               = var.ecpu_count
  data_storage_size_in_gb     = var.storage_gb
  is_auto_scaling_enabled     = false
  license_model               = "LICENSE_INCLUDED"
  admin_password              = var.admin_password
  is_mtls_connection_required = true
}

# INFRA-03(ORMワンクリック): mTLSウォレットを生成し、アプリが起動時に取得できるよう
# 非公開バケットへ載せるための base64 zip を出力する(wallet_password 指定時のみ)。
resource "oci_database_autonomous_database_wallet" "this" {
  count                  = var.generate_wallet ? 1 : 0
  autonomous_database_id = oci_database_autonomous_database.this.id
  password               = var.wallet_password
  generate_type          = "SINGLE"
  base64_encode_content  = true

  lifecycle {
    # prefix 変更 → db_name の in-place rename でサービス名(<dbname>_low)は変わるが
    # autonomous_database_id は不変なため wallet が再生成されず stale になり接続断(DPY-4000)。
    # db_name 変更時にウォレットを作り直す(docs/tips.md 2026-07-13 / PORT-01)。
    replace_triggered_by = [oci_database_autonomous_database.this.db_name]
  }
}
