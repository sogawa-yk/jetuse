variable "compartment_ocid" {
  type = string
}

variable "prefix" {
  type = string
}

variable "idcs_endpoint" {
  description = "Identity Domain の IDCS エンドポイント(https://idcs-xxxx.identity.oraclecloud.com)。OAuth の発行元かつ inbound 検証先"
  type        = string
}

variable "sdks" {
  description = <<-DESC
    配備する ReAct コンテナの SDK キー。OCIR の repo 名(<image_repo_prefix>-agent-<sdk>)と
    アプリ側の env 名(AGENT_<SDK>_APP_OCID)の両方に対応する。既定は 3SDK すべて(ADR-0019)。
  DESC
  type        = set(string)
  default     = ["openai", "langgraph", "adk"]
}

variable "image_registry" {
  description = "エージェント画像のレジストリ(例 ord.ocir.io/idqcucnenh88)。デプロイリージョンの OCIR を呼び出し元が渡す"
  type        = string
}

variable "image_repo_prefix" {
  description = "OCIR repo 名の接頭辞。release.yml の push 先(jetuse-agent-*)に合わせる"
  type        = string
  default     = "jetuse"
}

variable "image_tag" {
  description = "エージェント画像のタグ"
  type        = string
  default     = "latest"
}

variable "environment_variables" {
  description = "コンテナへ渡す環境変数(agent_common.py / agent_db.py が読む)。ADB パスワード等を含むため sensitive"
  type        = map(string)
  default     = {}
  sensitive   = true
}

variable "region" {
  description = "Hosted Application を作るリージョン。destroy 時のカスケード削除で control plane のエンドポイント組み立てに使う"
  type        = string
}

variable "min_replica" {
  description = <<-DESC
    アイドル時のレプリカ数。既定 0 = ゼロスケール(未使用なら課金されない・ADR-0019)。
    コールドスタートを避けたいデモでは 1 以上にする。
  DESC
  type        = number
  default     = 0
}

variable "max_replica" {
  type    = number
  default = 2
}

variable "target_concurrency_threshold" {
  description = "1レプリカあたりの目標同時実行数(CONCURRENCY スケーリング)"
  type        = number
  default     = 10
}
