variable "compartment_ocid" {
  description = "レジストリ用 Object Storage バケットを作成する jetuse-dev コンパートメント OCID(TF_VAR_ で渡す。コミットしない)"
  type        = string
  # plan/apply 出力での実値表示を抑止する(手動 REDACTED に依存しない)。
  sensitive = true
}

variable "prefix" {
  description = "リソース名プレフィックス(jetuse-dev では制約なし。既定 jetuse-registry)"
  type        = string
  default     = "jetuse-registry"
}

variable "region" {
  description = "Object Storage のリージョン識別子。読取 PAR の絶対 URL(取込ベースURL)組み立てに使う。**provider のリージョンと必ず一致させること**(バケットは provider のリージョンに作られるため、別値だと base URL が誤リージョンを指す)。既定は設けない(取り違え防止のため明示必須)"
  type        = string
}

variable "bucket_name" {
  description = "レジストリバケット名。空なら <prefix> をそのまま使う"
  type        = string
  default     = ""
}

variable "enable_versioning" {
  description = "オブジェクトのバージョニング(公開済みプラグイン版の不変性・監査のため既定 Enabled)"
  type        = bool
  default     = true
}

variable "enable_read_par" {
  description = "読取配布用 PAR(AnyObjectRead)を作るか。各 JetUse インスタンスが index.json/成果物を OCI 資格情報なしに取得する経路(comparison §2 方式A)"
  type        = bool
  default     = true
}

variable "read_par_expiry" {
  description = "読取 PAR の失効日時(RFC3339)。空なら apply 時刻 + read_par_expiry_days で算出する(固定日付の劣化回避)。明示指定で固定も可"
  type        = string
  default     = ""
}

variable "read_par_expiry_days" {
  description = "read_par_expiry を空にしたとき、apply 時刻から何日後を失効とするか(time_offset で確定。再 apply でも diff を出さない)"
  type        = number
  default     = 365
}
