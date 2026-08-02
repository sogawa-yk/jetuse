"""設定管理。環境変数 > .env。秘密値はコードに置かない。"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# エージェントのツール往復(ホップ)上限(AGT-04・ADR-0025)。1 ホップ = モデル 1 往復。
# **この 2 つの数値は 2026-08-02 の人間ゲートで承認済み**(ADR-0025 は Accepted)。
# 値を変えるのはこの 2 行だけ。機構(設定で変えられる / 天井超過は拒否 / 打ち切りを通知)は
# tasks/AGT-04.md で承認済みの決定。
# 天井は「モデルが同じツールを呼び続けても必ず止まる」ための硬い上限で、既定値ともども
# 根拠は ADR-0025。天井を超える値はクランプせず**拒否**する
# (黙って下げると「上げたのに効かない」が起きる — 解決は chat.resolve_max_tool_hops)。
AGENT_MAX_TOOL_HOPS_CEILING = 48
AGENT_MAX_TOOL_HOPS_DEFAULT = 24

# 文書検索(adb 経路の `rag_search`)の回数上限(AGT-05・ADR-0026)。**ホップとは別枠**で数える。
# 検索がホップを食うと「出典を細かくするほど業務 API に使える往復が減る」という不整合になる
# (実測: 予算 24 のうち 19〜22 が検索。業務 API に回せたのは 4〜5 回)。
# **2026-08-02 の人間ゲートで承認済み**(ADR-0026 §2 は Accepted)。
# 根拠は実測「1 API あたり 3〜5 回 × API 8 本 = 24〜40」。その**上端**を採る:
# 上限に達しても手続きは死なない(検索ツールを外して通知し、業務 API は続く)ので、
# **余らせるより届かせる側に倒す**ほうが、外したときの代償が小さい。
# 天井は既定の 2 倍。env `AGENT_MAX_DOC_SEARCHES` で変更できる。
AGENT_MAX_DOC_SEARCHES_DEFAULT = 40
AGENT_MAX_DOC_SEARCHES_CEILING = 80


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    oci_region: str = "ap-osaka-1"
    # OCI ログイン方式。env AUTH_MODE を読む（後方互換）。既定 config_file=ローカル ~/.oci/config。
    # config_file | resource_principal | instance_principal。解決は jetuse_core.oci_auth 経由。
    auth_mode: str = "config_file"
    # ~/.oci/config のプロファイル名（空=DEFAULT）。config_file モードで使用。
    oci_profile: str = ""
    compartment_ocid: str = ""
    project_ocid: str = ""
    # FIX-47: project_ocid 空のとき、compartment に ACTIVE project が無ければ自動作成を許可する。
    # 既定 false(検出のみ)。公開 ORM スタックは IAM policy とセットで true を注入する
    project_autocreate: bool = False

    # OpenSearch RAG(ENH-05)。例 http://10.1.1.x:9200。空ならOpenSearchバックエンド無効
    opensearch_endpoint: str = ""

    # AGT-04: エージェントのツール往復上限(env AGENT_MAX_TOOL_HOPS)。天井は
    # AGENT_MAX_TOOL_HOPS_CEILING。**あえて文字列で持ち、検証は
    # chat.resolve_max_tool_hops で行う**(空文字は未設定 = 既定値)。
    # int 宣言にすると `AGENT_MAX_TOOL_HOPS=abc` で Settings 生成そのものが失敗し、
    # get_settings() を呼ぶ**全 API**(チャット・RAG・認証依存)が 500 になる。
    # エージェント専用の設定ミスで壊すのは当該機能だけに閉じる。
    agent_max_tool_hops: str = ""

    # AGT-05: 文書検索の回数上限(env AGENT_MAX_DOC_SEARCHES)。既定は
    # AGENT_MAX_DOC_SEARCHES_DEFAULT。ホップ上限と同じ理由で**文字列で持ち**、
    # 検証は chat.resolve_max_doc_searches で行う(設定ミスで壊すのは当該機能だけ)。
    agent_max_doc_searches: str = ""

    # feature flags
    auth_required: bool = False  # INFRA-02(OIDC)完了までの暫定。本番はtrue必須

    # OIDC(IAM Identity Domain)。INFRA-02で確定する
    oidc_issuer: str = ""
    oidc_audience: str = ""
    oidc_jwks_url: str = ""

    # ADB接続(CHAT-02)。ウォレットは adb_wallet_dir(ローカル) か
    # 非公開バケット(adb_wallet_bucket/object)から起動時取得
    # アプリスキーマ(接続=DDL=マイグレーション先)。開発者ごとにE2E環境を分ける場合はここを変える
    adb_user: str = "JETUSE_APP"
    # 読取専用ユーザー(NL2SQL/データセット実行)。adb_userと対で分ける
    adb_query_user: str = "JETUSE_QUERY"
    adb_password: str = ""
    adb_dsn: str = ""  # 例: jetusedev_low
    adb_wallet_password: str = ""
    adb_wallet_dir: str = ""
    adb_wallet_bucket: str = ""
    adb_wallet_object: str = "adb_wallet.zip"
    # INFRA-03(ORM): バケット上のウォレットがbase64テキストならデコードして使う(Terraform配置)
    adb_wallet_base64: bool = False
    # INFRA-03(ORM): バケットにウォレットが無い場合、このADB OCIDからDatabase APIで生成して取得
    adb_ocid: str = ""

    # RAG(RAG-01): 原本バックアップ先バケット(空ならバックアップしない)
    rag_bucket: str = ""
    # RAG-03(Select AI): 索引のバケットURL組み立てに使用
    os_namespace: str = ""

    # NL2SQL(SQL-02): SemanticStore + 読取専用ユーザー
    semstore_ocid: str = ""
    adb_query_password: str = ""
    # Select AI クレデンシャル名。配備先(ORM/dev)も開発も ADB 自身の身分に統一した(ADR-0021)。
    # かつての既定 JETUSE_OCI_CRED は API キー焼き込み版で、これを作る経路は廃止済み
    # (＝既定のままだと存在しない資格情報を指す)。上書きは env SELECT_AI_CREDENTIAL。
    select_ai_credential: str = "OCI$RESOURCE_PRINCIPAL"

    # 議事録(VOICE-01): 音声と文字起こし結果のバケット(空なら機能無効=503)
    speech_bucket: str = ""
    # TTS(VOICE-03): 空=自動(デプロイリージョン → us-phoenix-1 の順に試行。FIX-58)。
    # かつてPhoenix限定だったが提供リージョンは拡大しており、決め打ちにするとPhoenix未購読の
    # テナンシで「デプロイ先では使えるのに落ちる」が起きる。明示指定時はそのリージョンのみ。
    tts_region: str = ""

    # SEC-02: 入力モデレーション(llama自己判定ガード)と管理者(カンマ区切りsub)
    moderation_enabled: bool = False
    admin_users: str = ""
    # GAP-01: OCIマネージド・ガードレールのプロンプトインジェクション検知
    prompt_injection_guard_enabled: bool = False
    # GAP-04: マネージド・ホスト型エージェント(IDCS OAuth=jetuse-agentを3コンテナで共用)
    hosted_agent_app_ocid: str = ""  # 旧サンプル(廃止)。後方互換のため残置
    hosted_agent_idcs_domain: str = ""
    hosted_agent_client_id: str = ""
    hosted_agent_client_secret: str = ""
    hosted_agent_scope: str = ""
    # AGT-MULTI(ADR-0009): 3SDK別ホスト型ReActコンテナのApplication OCID
    agent_openai_app_ocid: str = ""
    agent_langgraph_app_ocid: str = ""
    agent_adk_app_ocid: str = ""
    # PORT-03: このスタックがホスト型エージェントを配備する構成かどうか。
    # 「意図的に配備していない」と「配備したのに壊れている」を health で区別するために使う
    # (前者で /api/health 全体の ok を落とすと、エージェント不要のスタックが常時赤くなる)。
    hosted_agents_enabled: bool = False

    # OPS-02: OCI Logging(カスタムログOCID。空なら送らない) / Monitoring名前空間
    log_ocid: str = ""
    metrics_namespace: str = "jetuse_dev"

    log_level: str = "INFO"

    @property
    def inference_base_url(self) -> str:
        """推論系(DP)。Responses/Chat Completions/Files等(specs/00 未文書仕様1)"""
        return f"https://inference.generativeai.{self.oci_region}.oci.oraclecloud.com/openai/v1"

    @property
    def cp_base_url(self) -> str:
        """Vector Store本体CRUD(CP)。DPとはホストが異なる(specs/00 未文書仕様1)"""
        return f"https://generativeai.{self.oci_region}.oci.oraclecloud.com/20231130/openai/v1"


@lru_cache
def get_settings() -> Settings:
    return Settings()
