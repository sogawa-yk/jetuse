# 中央プラグインレジストリ(D2 / PLG-04)の保存層。
# ベンダー運用の共有 Object Storage バケットに index.json + 発行者公開鍵 + プラグイン成果物を保持する。
# publish(書込)はレジストリ Service がサーバ側 OCI 資格情報で行い、読取は各 JetUse インスタンスへ
# PAR(AnyObjectRead)で配布する(comparison/marketplace-plugin.md §2 方式A)。
#
# apply は人間ゲート(課金・jetuse-dev への実リソース作成承認)。エージェントは plan までに留める。

data "oci_objectstorage_namespace" "this" {
  compartment_id = var.compartment_ocid
}

locals {
  # namespace はテナンシ識別子。plan/ログに実値が出ないよう sensitive 化(参照先=バケット属性・
  # output すべてに伝播する)。
  ns     = sensitive(data.oci_objectstorage_namespace.this.namespace)
  bucket = var.bucket_name != "" ? var.bucket_name : var.prefix
  # 読取 PAR 失効: 明示指定があればそれ、無ければ apply 時刻 + N 日(time_offset で確定し再 apply でも不変)。
  par_expiry = var.read_par_expiry != "" ? var.read_par_expiry : (
    var.enable_read_par ? time_offset.par_expiry[0].rfc3339 : null
  )
}

# 読取 PAR の失効を apply 時刻からの相対で確定する(固定日付の劣化を回避)。base_rfc3339 は
# 作成時刻を state に保存するため、後続の plan/apply で perpetual diff にならない。
resource "time_offset" "par_expiry" {
  count       = var.enable_read_par && var.read_par_expiry == "" ? 1 : 0
  offset_days = var.read_par_expiry_days
}

# レジストリ本体バケット(非公開。書込はレジストリ Service 経由のみ)。
# バージョニングを有効化し、公開済みプラグイン版の上書き/誤削除に対する復旧余地を残す
# (版の不変性はレジストリ Service が (id,version) 一意で担保するが、保存層でも保全する)。
resource "oci_objectstorage_bucket" "registry" {
  compartment_id = var.compartment_ocid
  namespace      = local.ns
  name           = local.bucket
  access_type    = "NoPublicAccess"
  versioning     = var.enable_versioning ? "Enabled" : "Disabled"

  freeform_tags = {
    "app"       = "jetuse"
    "component" = "plugin-registry"
    "managedBy" = "terraform"
  }
}

# 読取配布用 PAR(AnyObjectRead)。各 JetUse インスタンスが index.json/成果物を OCI 資格情報なしに
# 取得する経路。bucket_listing_action は未指定=リスト不可(index.json を入口にする運用)。
# "Deny" を明示すると API が値を返さず毎 apply で再作成(URL 変化)になるため指定しない
# (object-storage モジュールと同じ既知の挙動)。
#
# 最小権限の前提(重要): AnyObjectRead はバケット内**全オブジェクト**を読取可能にする。よって本バケットは
# **公開配布物のみ**(index.json / プラグイン成果物 manifest / 発行者**公開**鍵)を置く前提で運用する。
# 秘密情報(発行者トークン・秘密鍵・内部メタデータ)は絶対に本バケットへ置かない(別バケット/Vault)。
# 公開鍵・manifest・index は本来公開物なので AnyObjectRead と整合する。読取配布が不要なら
# enable_read_par=false で PAR を作らず、レジストリ Service 経由の読取のみにできる。
resource "oci_objectstorage_preauthrequest" "registry_read" {
  count        = var.enable_read_par ? 1 : 0
  namespace    = local.ns
  bucket       = oci_objectstorage_bucket.registry.name
  name         = "${local.bucket}-read"
  access_type  = "AnyObjectRead"
  time_expires = local.par_expiry
}
