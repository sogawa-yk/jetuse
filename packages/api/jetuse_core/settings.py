"""設定管理。環境変数 > .env。秘密値はコードに置かない。"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    oci_region: str = "ap-osaka-1"
    compartment_ocid: str = ""
    project_ocid: str = ""

    # OpenSearch RAG(ENH-05)。例 http://10.1.1.x:9200。空ならOpenSearchバックエンド無効
    opensearch_endpoint: str = ""

    # feature flags
    auth_required: bool = False  # INFRA-02(OIDC)完了までの暫定。本番はtrue必須

    # SP3-03(ADR-0023 §3.5): 生成 SPA の app-session トークン(一回性コード/Cookie)の HMAC 秘密鍵。
    # 空 = fail-closed(AUTH=true で Cookie 認証経路を無効化)。環境依存の秘密値ゆえ .env 必須。
    app_session_secret: str = ""

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
    # SP2-02(specs/18 §3.1): アプリ全体の DP Files 総数上限(予約 ledger)。
    # 既定 None = 無制限(Public/main 互換・挙動不変)。Internal 配備で有効値(目安 2000)。
    rag_files_total_limit: int | None = None
    # SP2-02(specs/18 §3.1): デモ箱あたりの上限(超過 422 — 同期削除の所要を有界化)
    demo_max_rag_files: int = 20
    demo_max_datasets: int = 10
    # SP3-03/SP3-06(ADR-0023 §6・F2): フロント生成 LLM = 生成レジストリ(gen_models)のキー。
    # 既定 gpt-oss-120b。generate API の model 未指定時に使う。allowlist はレジストリが単一真実源
    # (署名プロキシ・runtime とも gen_models を参照 — 旧 GENAI_MODEL_ALLOWLIST は廃止)。
    generation_model: str = "gpt-oss-120b"
    # SP3-06: ORASEJAPAN 共有テナンシ(生成 gpt-5 系)の auth プロファイルと compartment OCID。
    # 環境依存値ゆえ .env(コミット禁止)。空 = 共有テナンシモデルは使用不可(fail-closed)。
    gen_shared_profile: str = ""
    gen_shared_compartment_ocid: str = ""
    # SP3-09: デプロイ環境の鍵材料 = Vault シークレット(JSON)の OCID(非鍵材料)。
    # 設定時は GEN_SHARED_PROFILE より優先(gen_shared_vault — RP で取得・in-memory 署名)。
    gen_shared_secret_ocid: str = ""
    # SP3-03(§4.2 N3): 同時 provisioning デモ数の上限。固定名グローバルロック下で数える。
    demo_max_concurrent_generations: int = 2
    # SP3-03(specs/19 §4.2 N7・ADR-0023): 1 生成の壁時計上限(秒)。runtime のハードキル。
    generation_timeout_s: int = 900
    # SP3-03(ADR-0023 §1 の 2 相分離): 非信頼生成相の使い捨て podman コンテナ。鍵レス(OCI 認証を
    # 渡さず egress は署名プロキシ経由)+ N7 資源上限。環境依存の到達/資源値(承認済み緩和で単純化)。
    # 版数タグ(opencode 版)で固定 — stale な別版イメージを掴んで N6 メタと食い違わせない(#194)
    generation_image: str = "jetuse-demo-gen:oc1.17.15"
    # コンテナから見た署名プロキシ(鍵レス egress)。環境依存の実エンドポイントゆえ .env 必須
    # (既定は空 = 生成開始時に fail-fast。エンドポイント実値をコードにコミットしない)。
    generation_proxy_url: str = ""
    generation_container_network: str = "slirp4netns:allow_host_loopback=true"
    generation_cpus: str = "1"
    generation_memory: str = "4g"
    generation_pids_limit: int = 256
    generation_scaffold_dir: str = ""  # 空なら repo 既定(spikes/sp3_03_scaffold)
    # SP3-08(ADR-0023 §1 B'): 生成 runtime バックエンド。podman = ローカル開発(従来)、
    # oci-ci = 生成ごとの使い捨て Container Instance(デプロイ環境の正 — dev-app tf が設定)。
    generation_runtime: str = "podman"
    # oci-ci 用の配線(dev-app tf が env で与える。未設定は生成開始時に fail-fast)
    generation_ci_subnet_ocid: str = ""   # 生成 CI を置く private サブネット
    generation_ci_ad: str = ""            # availability domain 名
    generation_gen_image_url: str = ""    # 相1 生成イメージ(OCIR public repo)
    generation_build_image_url: str = ""  # 相2 信頼ビルドイメージ(OCIR public repo)
    # 相ごとのタイムアウト(ADR-0023 §1: 相1 9 分・相2 2 分。全体は generation_timeout_s)
    generation_ci_gen_timeout_s: int = 540
    generation_ci_build_timeout_s: int = 120
    # SP2-02(specs/18 §3.1): 起動世代トークン。entrypoint.sh が bootstrap/uvicorn 起動前に
    # export し、両プロセスで共有する。upload gate は「今回起動の reconcile が開けた」場合のみ
    # 通す(前回起動の 'Y' が残っていても boot_id 不一致で fail-closed — codex review-8 B001)。
    # 空(単一プロセス/未設定)なら boot 照合はスキップ(gate 値のみ)。
    app_boot_id: str = ""
    # SP2-02(specs/18 §4.3): VPD(行レベル分離)を有効化するか。既定 False = Public/main 互換
    # (VPD は Internal/デモテナンシ機能。未配備環境で integrity_gate/apply_policy を強制すると
    # 従来デプロイの dataset 作成・dbchat が壊れる — codex review-10 B004)。Internal/デモ配備は
    # 明示的に True(かつ人間ゲートで VPD セットアップ済み)にする。
    vpd_enabled: bool = False

    # NL2SQL(SQL-02): SemanticStore + 読取専用ユーザー
    semstore_ocid: str = ""
    adb_query_password: str = ""
    # Select AI クレデンシャル名。ORM/RP環境は OCI$RESOURCE_PRINCIPAL(INFRA-03)
    select_ai_credential: str = "JETUSE_OCI_CRED"

    # 議事録(VOICE-01): 音声と文字起こし結果のバケット(空なら機能無効=503)
    speech_bucket: str = ""
    # TTS(VOICE-03): Phoenix限定(SPIKE-06)。クロスリージョン呼び出し
    tts_region: str = "us-phoenix-1"

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
