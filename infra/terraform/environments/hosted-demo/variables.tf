# DEP-01: 生成デモのコンテナ配備(L3 ホスト型)環境変数。
# 構成由来の値(prefix/image_url/environment_variables/...)は deploy.py が生成する
# `*.auto.tfvars.json`(ContainerDeploySpec.render_tfvars_json)で与える。基盤由来の値
# (compartment/subnet/nsg)は固定リファレンス基盤が供給し、TF_VAR_ か別 tfvars で与える。
# 実値(OCID/秘密)はコミットしない(CLAUDE.md / ADR-0011)。

variable "region" {
  type    = string
  default = "ap-osaka-1"

  # ADR-0011: 配備は ap-osaka-1 固定(OCIR=kix・jetuse-dev も ap-osaka)。別リージョン provider で
  # kix イメージ/OCI_REGION=ap-osaka-1 の spec を plan/apply させない(deploy.py の固定と整合)。
  validation {
    condition     = var.region == "ap-osaka-1"
    error_message = "region は ap-osaka-1 固定です(ADR-0011)。"
  }
}

variable "compartment_ocid" {
  description = "jetuse-dev コンパートメント OCID(TF_VAR_compartment_ocid で渡す。コミットしない)"
  type        = string
}

# --- 基盤由来(固定リファレンス基盤が供給する infra 値) -------------------------

variable "subnet_id" {
  description = "デモコンテナを置くプライベートサブネット OCID(既存基盤の private subnet)"
  type        = string
}

variable "nsg_id" {
  description = "コンテナ VNIC に付ける NSG OCID(既存基盤の app NSG)"
  type        = string
}

# --- 構成由来(deploy.py の ContainerDeploySpec から生成) ---------------------

variable "prefix" {
  description = "display_name 接頭辞(<prefix>-api)"
  type        = string
}

variable "image_url" {
  description = "OCIR(ap-osaka-1, public)イメージ URL(ADR-0011)"
  type        = string

  # 多層防御: deploy.py と同一正規表現で ap-osaka OCIR(kix)固定。空白/空セグメント/末尾スラッシュも拒否。
  validation {
    condition     = can(regex("^kix\\.ocir\\.io/[^/\\s:]+(/[^/\\s:]+)+(:[^\\s:]+)?$", var.image_url))
    error_message = "image_url は kix.ocir.io/<ns>/<repo>[:tag] 形式で(ADR-0011)。"
  }
}

variable "app_port" {
  type    = number
  default = 8000

  # 多層防御(deploy.py と同等): 1..65535 の整数。
  validation {
    condition     = var.app_port >= 1 && var.app_port <= 65535 && floor(var.app_port) == var.app_port
    error_message = "app_port は 1..65535 の整数。"
  }
}

variable "ocpus" {
  type    = number
  default = 1

  # 多層防御(deploy.py と同等): 1..64 の整数刻み(端数/過大を弾く=コスト境界)。
  validation {
    condition     = var.ocpus >= 1 && var.ocpus <= 64 && floor(var.ocpus) == var.ocpus
    error_message = "ocpus は 1..64 の整数。"
  }
}

variable "memory_gb" {
  type    = number
  default = 8

  # 多層防御(deploy.py と同等): 1..1024 の整数刻み。
  validation {
    condition     = var.memory_gb >= 1 && var.memory_gb <= 1024 && floor(var.memory_gb) == var.memory_gb
    error_message = "memory_gb は 1..1024 の整数。"
  }
}

variable "environment_variables" {
  description = "非秘密の環境変数(deploy.py が生成)。秘密はここに入れない。"
  type        = map(string)
  default     = {}

  # 多層防御: 秘密(broker 署名鍵・コンテナ OIDC 秘密)を非秘密 env から注入させない(ADR-0014)。
  # 秘密(Vault OCID)は本環境で扱わない(DEP-02 注入)。env は非秘密のみで衝突も起きない。
  validation {
    condition = length(setintersection(
      keys(var.environment_variables),
      ["PLATFORM_BROKER_SECRET", "HOSTED_AGENT_CLIENT_SECRET"]
    )) == 0
    error_message = "秘密(PLATFORM_BROKER_SECRET / HOSTED_AGENT_CLIENT_SECRET)は environment_variables 不可(DEP-02 注入)。"
  }
  # 多層防御(deploy.py の _ENV_NAME_RE と同じ): env 名は英大文字/数字/アンダースコアのみ。
  validation {
    condition = alltrue([
      for k in keys(var.environment_variables) : can(regex("^[A-Z_][A-Z0-9_]*$", k))
    ])
    error_message = "environment_variables のキーは英大文字/数字/アンダースコアのみ。"
  }
  # 名前空間 allowlist(deploy.py と同方針): コンテナ env キーは OCI_REGION か JETUSE_* のみ。
  # これにより DB_PASSWORD/DB_PASS/OPENAI_KEY/SLACK_TOKEN 等の**任意の資格情報キー**に実値を入れて
  # L3 へ渡す経路を、denylist ではなく構造的に閉じる(秘密は本環境で扱わない=DEP-02。D5)。
  validation {
    condition = alltrue([
      for k in keys(var.environment_variables) : k == "OCI_REGION" || startswith(k, "JETUSE_")
    ])
    error_message = "environment_variables のキーは OCI_REGION か JETUSE_ 接頭辞のみ(秘密は DEP-02 注入)。"
  }
  # 名前空間内でも資格情報名(JETUSE_DB_PASS/JETUSE_OPENAI_KEY 等)を弾く(deploy.py の hint と同パターン)。
  validation {
    condition = alltrue([
      for k in keys(var.environment_variables) :
      !can(regex("(?i)SECRET|PASS|PWD|TOKEN|CREDENTIAL|PRIVATE|KEY|AUTH|CERT|SIGNATURE|DSN|DATABASE_URL|CONNECTION_STRING", k))
    ])
    error_message = "資格情報らしいキーは environment_variables 不可(秘密は DEP-02 注入)。"
  }
  # OCI_REGION は **必須かつ ap-osaka-1 固定**(ADR-0011)。deploy.py は常に付与する。手書き tfvars で
  # 省略して実行時リージョン境界をすり抜けることも禁ずる(キー欠落＝lookup 既定""≠ap-osaka-1 で検証失敗)。
  validation {
    condition     = lookup(var.environment_variables, "OCI_REGION", "") == "ap-osaka-1"
    error_message = "environment_variables に OCI_REGION=ap-osaka-1 が必須(ADR-0011)。"
  }
  # 多層防御(deploy.py の _validate_extra_environment と同等): env の**値**に Vault secret OCID を
  # 入れさせない(JETUSE_REF="ocid1.vaultsecret..." 等)。秘密は env で運ばない=DEP-02 注入。
  validation {
    condition = alltrue([
      for v in values(var.environment_variables) : !can(regex("ocid1\\.vaultsecret\\.", v))
    ])
    error_message = "environment_variables の値に Vault secret OCID を入れない(秘密は DEP-02 注入)。"
  }
}

# 注: 秘密(Vault OCID 参照含む)は本環境では受けない。コンテナへの秘密注入は DEP-02(Platform API 注入)が
# 担い、Vault OCID を tfvars/Terraform state に残さない設計とした(DEP-01 は配備=コンテナ起動まで)。
# image pull も ADR-0011 の public OCIR を前提とし、pull secret(Vault OCID)を本環境で扱わない。
