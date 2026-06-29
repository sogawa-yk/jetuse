"""設定管理。環境変数 > .env。秘密値はコードに置かない。"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    oci_region: str = "ap-osaka-1"
    compartment_ocid: str = ""
    project_ocid: str = ""

    # sample-app(SBA)スロット実行の既定モデル(SBA-02)。Web UI は model を送らないため、
    # ここが既定実行経路。追加設定なしでデモが動くよう **project_ocid 不要な chat completions 系**
    # を既定にする(Responses 系 gpt-oss-120b は project_ocid 必須)。env で上書き可。
    sample_app_model: str = "llama-3.3-70b"
    # BE-07: sample-app スロット内 RAG の semantic/vector retrieval を有効化する。
    # 既定 False = 従来の語彙重なりスコア(ベクトル未設定でも素デプロイで動く)。
    # True にすると既存 OCI 埋め込み(embeddings.embed / cohere.embed-multilingual-v3.0)を
    # 再利用してコサイン類似で検索し、埋め込み呼び出し失敗時は自動で従来スコアへ degrade する。
    sample_app_semantic_retrieval: bool = False

    # OpenSearch RAG(ENH-05)。例 http://10.1.1.x:9200。空ならOpenSearchバックエンド無効
    opensearch_endpoint: str = ""

    # プラグイン中央レジストリ(PLG-03/D2)。ベンダー運用 Object Storage の index.json を指す
    # ベースURL(末尾スラッシュ任意。例 https://<ns>.objectstorage.<region>.oci.../jetuse-registry/)。
    # 空ならレジストリ機能無効(取込はベースURLを明示指定したときのみ)。実値は .env で与え、
    # コミットしない(OCID/エンドポイント実値の混入防止)。
    plugin_registry_url: str = ""

    # プラグイン公開(PLG-05/D7)。インスタンスが「発行者」として中央レジストリへ publish するための
    # 資格情報。すべて .env / Vault で注入し、コミットしない(鍵・トークン実値の混入防止)。いずれかが
    # 空なら publish 機能は無効(/api/.../publish は 503 を返す)。
    registry_publish_url: str = ""        # publish API のベースURL(例 https://registry.example)
    registry_publisher_id: str = ""       # 発行者ID(manifest.publisher・レジストリ認証と対応)
    registry_publisher_token: str = ""    # publish 用 Bearer トークン(平文。Vault/.env 管理)
    registry_signing_key: str = ""        # base64(32バイト ed25519 秘密シード)。manifest 署名鍵
    registry_public_key_id: str = ""      # 署名鍵に対応する公開鍵ID(レジストリ登録名)
    registry_namespace: str = ""          # plugin id の名前空間。空なら publisher_id を使う
    registry_min_version: str = ""        # manifest.jetuse.minVersion。空なら publisher 既定(0.3.0)

    # feature flags
    auth_required: bool = False  # INFRA-02(OIDC)完了までの暫定。本番はtrue必須

    # OIDC(IAM Identity Domain)。INFRA-02で確定する
    oidc_issuer: str = ""
    oidc_audience: str = ""
    oidc_jwks_url: str = ""

    # Platform API ブローカー(PAPI-01/ADR-0014)。スコープ付き短期トークンの署名鍵。
    # JetUse が発行=検証する閉じた境界なので対称鍵(HS256)。**コミットしない**(.env/Vault 注入)。
    # 空ならブローカーは fail-closed: 発行・検証とも不可(安全側に閉じる)。
    platform_broker_secret: str = ""
    # 発行する短期トークンの既定 TTL(秒)。短く保ち失効を時間で担保する(ADR-0014)。
    platform_token_ttl_seconds: int = 300

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
    # SBA-B(SBA-03): sample-app NL2SQL の照会対象スキーマ。設定時、専用 execute は実行接続の
    # CURRENT_SCHEMA をこの値に固定し、非修飾テーブル名が当該スキーマの物理表へ確定解決する
    # (synonym 依存や読取ユーザ側の同名オブジェクトに左右されない)。空なら従来どおり既定解決。
    sample_db_schema: str = ""
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

    # DEP-01: 生成デモのコンテナ配備(L3)既定イメージ。ADR-0011 に従い OCIR(ap-osaka-1, public)。
    # namespace はテナンシ固有のため実値は .env で与え、コミットしない。空なら配備仕様生成時に
    # image_url を明示指定する必要がある(未指定は fail-closed で DeploySpecError)。
    hosted_demo_image_url: str = ""

    # DEP-02: 生成デモコンテナへ注入する Platform API のベース URL(L3 ランタイム注入)。
    # https 固定。デモコンテナはこの URL ＋ ブローカー発行の短期トークンでテナントデータへ到達する
    # (DB 認証情報は持たない / ADR-0014 D5)。空なら注入組み立て時に明示指定が必要(未指定は
    # fail-closed で InjectionError)。環境依存実値は .env で与え、コミットしない。
    platform_api_base_url: str = ""

    # BE-01: 生成デモの OKE 実配備配線（launch → kubectl apply）。
    # 既定 OFF = **後方互換**（launch は従来どおり DB 行＋/sba URL のみ。コンテナ配備しない）。
    # True にすると launch が build_deploy_spec→render→（要すれば）build_runtime_injection→
    # kubectl apply まで実行する（描画側の fail-closed はそのまま流す）。
    oke_deploy_enabled: bool = False
    # kubeconfig パス（環境依存実値。.env で与え、コミットしない）。空なら kubectl 既定
    # （KUBECONFIG / ~/.kube/config）に委ねる。
    kube_config_path: str = ""
    # True なら apply を `kubectl --dry-run=client` で **検証のみ**（実ワークロードを作らない）。
    # 実 OKE への apply/課金は人間ゲート。自走（オーケストレータ）は dry-run 検証までに留める。
    # 実配備するときだけ人間が False にする（= 人間ゲートを越える明示操作）。
    oke_deploy_dry_run: bool = True

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
