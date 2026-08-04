"""リクエスト/レスポンスDTO(Pydantic)。

service/main.py から分離(P1c)。route schema と service層 validator の両方から
import される。`validated()` は service/validators.py 側の純粋関数へ委譲し、
ここでは薄いメソッドとして残す(後方互換 — main.py からの import を維持)。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from jetuse_core import http_tools, rag_metadata, settings, tts

from .validators import validate_agent_definition, validate_usecase_definition


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    """`POST /api/chat/stream`（およびデモスコープの `/api/demos/{demo_id}/chat`）の入力。

    description は **API-01**: この API の主な利用者はコーディングエージェントであり、
    型だけでは使い分けを推測（＝でっち上げ）してしまうため、各パラメータに
    「いつ使うか」と「他とどう干渉するか」を書いてある。例は `examples` を見る。
    """

    # 例は CHAT_REQUEST_EXAMPLES（このファイル末尾）が単一の正本。**schema 側は生の値の配列**
    # (JSON Schema の `examples` は値そのものを並べる — 名前つきの Example Object を schema に
    # 置くと、利用者や生成器には `summary`/`value` というキーを持つリクエストが例として見える。
    # review-6 F-003)。名前と説明つきの例はルート側の requestBody に載せる(OpenAPI Example Object)。
    model_config = ConfigDict(
        json_schema_extra=lambda schema: schema.update(
            examples=[e["value"] for e in CHAT_REQUEST_EXAMPLES.values()]
        )
    )

    model: str = Field(
        description="使うモデルのキー。**選べる値は `GET /api/chat/models` の `key`**"
        "（内部 ID を書くと 400 `unknown model`）。エージェント・文書検索・画像入力は"
        "Responses 系モデルが要る（同エンドポイントの `api` / `agent` / `vision` を見る）。",
        examples=["gpt-oss-120b"],
    )
    messages: list[ChatMessage] = Field(
        min_length=1,
        description="会話履歴。**毎回すべて送る**のが基本（`conversation_id` を使う場合を除く）。"
        "`system` で役割を与え、最後は通常 `user`（画像を付ける場合は最後が `user` 必須）。",
    )
    temperature: float | None = Field(
        default=None, ge=0, le=2,
        description="ばらつき。**未指定＝モデル既定**（推奨）。"
        "事実性を上げたいときだけ 0 付近に下げる。",
    )
    # 生成パラメータ拡張(CHAT-04b)。未指定はAPIに渡さない=モデル既定
    top_p: float | None = Field(
        default=None, gt=0, le=1,
        description="核サンプリング。`temperature` と**両方いじらない**（片方だけ調整する）。",
    )
    max_tokens: int | None = Field(
        default=None, ge=1, le=32768,
        description="生成の上限トークン。長い出力が途中で切れるときに上げる。"
        "推論モデルでは小さすぎると本文が空になる（推論に消費される）。",
    )
    reasoning_effort: Literal["low", "medium", "high"] | None = Field(
        default=None,
        description="推論の深さ（対応モデルのみ）。難しい判断・多段の計算で `high`、"
        "定型の書き換えなら `low` で速くなる。",
    )
    conversation_id: str | None = Field(
        default=None,
        description="`POST /api/conversations` で作った会話の id。指定すると履歴が ADB に"
        "永続化され、次回は差分だけ送れる。**単発の呼び出しでは不要**。",
    )
    persist_user: bool = Field(
        default=True,
        description="ユーザー発話を保存するか。**再生成（同じ質問をやり直す）ときだけ false**"
        "にして二重保存を防ぐ。`conversation_id` が無いときは効果なし。",
    )
    # file_searchツール接続(RAG-02。Responses系のみ)。description は API-01: 併用不可を
    # 呼ぶ前に知れるようにする(呼んでから 400 で知る状態をやめる)
    rag: bool = Field(
        default=False,
        description="アップロード済み文書への検索(file_search)を有効にする。"
        "**`agent` / `agent_id` とは併用できない**(400 `agent and rag cannot be combined`)。"
        "エージェントに文書検索させたい場合はこれではなく "
        "`agent=true` + `enabled_tools=[\"rag_search\"]`。Responses 系モデルのみ。",
    )
    # RAG-03/ENH-05/RAGM-02(adb=Oracle AI Database 自前索引・チャンク単位の出典)
    rag_backend: Literal["vector_store", "select_ai", "opensearch", "adb"] = Field(
        default="vector_store",
        description="`rag=true`(非エージェント)のときの検索バックエンド。"
        "**エージェントの文書検索のバックエンドは別パラメータ `agent_rag_backend`** "
        "(名前が似ているので注意。エージェントモードでこちらを指定しても効かない)。"
        "能力差は `GET /api/capabilities` の `rag.search.backend_capabilities` を見る。",
    )
    # RAGM-01: file_searchのメタデータ絞り込み(例 {"type":"eq","key":"current_version",
    # "value":"Y"} で旧版を検索から外す)。vector_storeバックエンドのみ。
    rag_filters: dict | None = Field(
        default=None,
        description="file_search の属性フィルタ(例 "
        '`{"type":"eq","key":"current_version","value":"Y"}`)。'
        "`rag=true` かつ `rag_backend=vector_store` かつ非エージェントのときだけ有効"
        "(それ以外は黙って無視せず 400)。",
    )
    # エージェントモード(AGT-01)。tool_resultsは承認フローの継続時に使用
    agent: bool = Field(
        default=False,
        description="エージェントモード(ツールを自律的に使う)。**`rag` とは併用できない**"
        "(400 `agent and rag cannot be combined`)。**文書検索させるには "
        "`enabled_tools` に `rag_search` を入れる**(入れないと検索されない)。",
    )
    # AGT-04: エージェントの文書検索(rag_search)のバックエンド。既定は現行と同じ
    # file_search built-in(出典はファイル単位)。adb はチャンク単位の出典
    # (シート名・セル範囲)を返す。`rag=true` との併用禁止は据え置き(別タスク)
    agent_rag_backend: Literal["vector_store", "adb"] = Field(
        default="vector_store",
        description="エージェントの文書検索ツール(`rag_search`)のバックエンド。"
        "`vector_store`(既定)=出典はファイル単位 / `adb`=チャンク単位の出典(シート名・セル範囲)。"
        "**`adb` を選ぶときだけ追加の条件がある**: `agent=true` 必須"
        "(400 `agent_rag_backend requires agent mode`)・保存済みエージェント(`agent_id`)には"
        "指定できない(400 `... is not supported for saved agents`)・`enabled_tools` に "
        "`rag_search` が必要(400 `agent_rag_backend requires rag_search in enabled_tools`)。"
        "既定値 `vector_store` は明示しても検査されない(未指定と同じ扱い)。"
        "`rag=true` 側の設定は `rag_backend`(別物)。",
    )
    # AGT-04: このターンのツール往復上限。未指定は設定値(AGENT_MAX_TOOL_HOPS)。
    # 天井を超える値は 422(クランプしない — ADR-0025)
    # bool は int の派生なので、素の int だと JSON の `true` が 1 として通る。
    # 上限の指定に真偽値が来るのは誤りなので API 境界で断る(resolve_max_tool_hops の
    # bool 拒否と挙動を揃える — 片方だけ厳しいと、どちらが正か読めなくなる)
    max_tool_hops: StrictInt | None = Field(
        default=None, ge=1, le=settings.AGENT_MAX_TOOL_HOPS_CEILING,
        description="このターンのツール往復(ホップ)上限。**未指定＝サーバー設定の既定**で足りる。"
        "多段の業務フロー(API を順に何本も呼ぶ)で「打ち切り」が出るときだけ上げる。"
        "`agent=true` 必須・`agent_id` には指定できない(400)。天井超えは 422(丸めない)。"
        f"上限の天井は {settings.AGENT_MAX_TOOL_HOPS_CEILING}。文書検索はこの予算に含まれない"
        "(別枠 — ADR-0026)。",
    )
    auto_tools: bool = Field(
        default=False,
        description="ツール実行を JetUse 側で自動実行するか。**無人で回すなら true**。"
        "false のときは SSE に承認イベントが出るので、`POST /api/agent/execute-tool` で"
        "実行して結果を次の呼び出しの `tool_results` に載せて続ける(人間の確認を挟む形)。",
    )
    # AGT-04: 承認往復の継続で送り返すツール結果。ホップ上限の天井まで受ける
    # (ここが天井より小さいと、上限を上げても承認モードだけ 422 で継続できない)
    # AGT-05: 文書検索はホップの予算から外れたので、ここを 48 のままにすると
    # 検索を挟む承認往復が予算判定に届く前に 422 で詰まる(review-2 の指摘)。
    # **これは予算の上界ではなく要求ボディの安全弁**である —— 1 往復から複数の
    # function_call が返りうるので、件数はホップ数からは決まらない(AGT-01d からの既存の
    # 性質で、従来の 48 も上界ではなかった)。検索を別枠にしたぶん枠を広げただけで、
    # 実際の歯止めは stream_agent 側の 2 つの予算が持つ。
    tool_results: list[dict] | None = Field(
        default=None,
        max_length=(
            settings.AGENT_MAX_TOOL_HOPS_CEILING + settings.AGENT_MAX_DOC_SEARCHES_CEILING
        ),
        description="**承認往復の継続**でだけ使う。承認イベントの `call` と、"
        "`POST /api/agent/execute-tool` の結果を対にして送り返すと、エージェントが続きから走る。"
        "新規の質問では送らない。",
    )
    enabled_tools: list[str] | None = Field(
        default=None,
        max_length=20,
        description="このターンでエージェントに許すツール名。**文書検索は `rag_search` を"
        "明示的に入れないと行われない**(入れ忘れても成功するが検索されない)。"
        "`agent_id` 指定時はエージェント定義側の `enabled_tools` が使われる。"
        "指定できるツール名は `GET /api/agent/tools`。",  # AGT-01b
    )
    mcp_server_ids: list[str] | None = Field(
        default=None, max_length=5,
        description="登録済み MCP サーバーの id(`GET /api/agent/mcp-servers`)。"
        "**MCP で公開されている外部ツール群**を使わせたいときに渡す。`agent=true` のときだけ効く。",
    )  # AGT-02
    # TOOL-01: 登録済み外部HTTPツールのid。1エージェントに渡せる数はモデルの選択精度の
    # ためMAX_TOOLS_PER_AGENTで頭打ちにする
    http_tool_ids: list[str] | None = Field(
        default=None, max_length=http_tools.MAX_TOOLS_PER_AGENT,
        description="登録済み外部 HTTP ツールの id(`GET /api/agent/http-tools`)。"
        "**デモ側の業務 API をエージェントに呼ばせたいとき**に使う"
        "(先に `POST /api/agent/http-tools` で name/description/JSON Schema/URL を登録する)。"
        "1 件でも解決できなければ 404 で止まる(黙って外さない)。`agent=true` のときだけ効く。"
        f"1 エージェントあたり最大 {http_tools.MAX_TOOLS_PER_AGENT} 件。",
    )
    agent_id: str | None = Field(
        default=None,
        description="保存済みエージェント定義の id(`GET /api/agents`)。"
        "**定義側の model / instructions / enabled_tools が使われる**ので、"
        "この指定時は `model` は無視され、`max_tool_hops` / `agent_rag_backend` は"
        "指定できない(400)。単発で組み立てるなら `agent=true` + `enabled_tools`。",
    )  # AGT-03: エージェント定義の適用
    # 画像入力(MM-01): data URI。最終userメッセージに適用(当該ターンのみ・永続化なし)
    # 上限10枚=映像分析のフレーム数を許容(チャットUIは4枚に制限)
    images: list[str] | None = Field(
        default=None, max_length=10,
        description="画像入力(`data:image/...;base64,` の data URI・最大 10 枚)。"
        "**vision 対応モデルのみ**で、`agent` / `rag` とは併用できない(422)。"
        "最終メッセージは `user` であること。1 枚 2MB / 合計 10MB 超で 413。"
        "このターンだけに適用され保存されない。",
    )
    # 監査の機能ラベル(SEC-02。例: usecase:<id> / video / voicechat)
    source: str | None = Field(
        default=None, max_length=80, pattern=r"^[a-zA-Z0-9:_-]+$",
        description="監査ログに残す機能ラベル(例 `usecase:<id>` / `video` / `voicechat`)。"
        "**デモを作る側が「どの画面からの呼び出しか」を後で追えるようにする**ための任意項目。",
    )
    # Agents SDK承認往復(FW-01b): 中断時のsdk_stateを返送し、call_id→可否を添える
    sdk_state: str | None = Field(default=None, max_length=2_000_000)
    sdk_approvals: dict[str, bool] | None = None

    @field_validator("rag_filters")
    @classmethod
    def _check_rag_filters(cls, v: dict | None) -> dict | None:
        """RAGM-01: 未知キーは上流でエラーにならず0件になる(SPIKE-M1 ①-b)ため
        ここで弾く(422)。既知フィールドだけに正規化して通す。"""
        try:
            return rag_metadata.validate_filters(v)
        except rag_metadata.MetadataError as e:
            raise ValueError(str(e)) from e


# 例の単一の正本(API-01)。**実際に受理される組み合わせだけ**を置く
# (tests/test_openapi_spec.py が実ルートへ流して 4xx にならないことを検査する)。
# schema 側は値だけを使い、ルート側は summary/description つきでそのまま載せる。
CHAT_REQUEST_EXAMPLES: dict[str, dict] = {
    "plain_chat": {
        "summary": "素のチャット（最小）",
        "description": "モデルと会話履歴だけ。SSE でトークンが逐次届く。",
        "value": {
            "model": "gpt-oss-120b",
            "messages": [{"role": "user", "content": "OCI の利点を3つ挙げて"}],
        },
    },
    "agent_with_document_search": {
        "summary": "エージェント実行（文書検索あり）",
        "description": "エージェントに手元の文書を検索させる正しい形。`rag` は使わない"
                       "（併用は 400）。`enabled_tools` に `rag_search` を入れないと"
                       "検索は行われない。",
        "value": {
            "model": "gpt-oss-120b",
            "messages": [{"role": "user", "content": "社内規程では経費精算の締め日は?"}],
            "agent": True,
            "enabled_tools": ["rag_search"],
            "auto_tools": True,
        },
    },
    "document_qa": {
        "summary": "文書 Q&A（エージェントなし）",
        "description": "アップロード済み文書に基づく引用付き回答。`agent` は使わない"
                       "（併用は 400）。事前に `POST /api/rag/files` で文書を入れておく。",
        "value": {
            "model": "gpt-oss-120b",
            "messages": [{"role": "user", "content": "保証期間は何年?"}],
            "rag": True,
            "rag_backend": "vector_store",
        },
    },
}


class ConversationCreate(BaseModel):
    model: str
    title: str | None = None


class Nl2SqlRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    # SQL-04比較モード。web UI(dbchat.tsx)は常にbackendを明示送信し既定値は"sql_search"
    # のため、「未指定」と「明示sql_search」をワイヤ上で区別できない(対象areaはpackages/api
    # のためUI側の変更はこのタスクでは行わない)。よって"sql_search"はどちらの場合も
    # SEMSTORE_OCID未設定なら既定機能(dbchatが別テナンシで必ず壊れる問題の根治)を優先し
    # select_aiへ自動切替する(下記PORT-02コメント参照)。
    # ponytail: この結果SQL-04比較モードはSEMSTORE_OCID未設定環境では両パネルがselect_ai
    # になり得る既知の制約。UI側がbackend="auto"相当を明示送信できるようになれば
    # sql_search側を強制する経路を分離できる(docs/tips.md参照)。
    backend: Literal["sql_search", "select_ai"] = "sql_search"
    target: Literal["sample", "datasets"] = "sample"  # ENH-01: SHサンプル or 本人CSV
    model: str | None = Field(default=None, max_length=100)  # feedback 20260620 #3: モデル選択


class GenerateDatasetRequest(BaseModel):
    description: str = Field(min_length=1, max_length=2000)  # どんなデータか
    display_name: str | None = Field(default=None, max_length=200)
    rows: int = Field(default=30, ge=1, le=200)
    model: str | None = Field(default=None, max_length=100)  # feedback 20260620 #3


class SeedDatasetsRequest(BaseModel):
    model: str | None = Field(default=None, max_length=100)  # feedback 20260620 #12/#3


class MinutesGenerateRequest(BaseModel):
    template: Literal["minutes", "faq", "article"] = "minutes"  # VOICE-01
    model: str = "gpt-oss-120b"


class SttSessionCreate(BaseModel):
    language: str = Field(default="ja", pattern=r"^[a-z]{2,3}(-[A-Z]{2})?$")  # VOICE-02


class TtsRequest(BaseModel):
    text: str = Field(min_length=1, max_length=tts.MAX_TEXT_CHARS)  # VOICE-03
    voice: str = tts.DEFAULT_VOICE


class TranslateRequest(BaseModel):  # ENH-10
    text: str = Field(min_length=1, max_length=4000)
    target: str = Field(min_length=2, max_length=8)
    source: str | None = Field(default=None, max_length=8)
    backend: Literal["llm", "oci_language"] = "llm"


class ExecuteSqlRequest(BaseModel):
    sql: str = Field(min_length=1, max_length=20000)


class AgentDefinition(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    icon: str | None = Field(default=None, max_length=16)
    instructions: str = Field(min_length=1, max_length=20000)
    model: str
    enabled_tools: list[str] = Field(default_factory=list, max_length=20)
    mcp_server_ids: list[str] = Field(default_factory=list, max_length=5)
    project_ocid: str | None = Field(default=None, max_length=255)
    visibility: Literal["private", "public"] = "private"
    tags: list[str] = Field(default_factory=list, max_length=10)
    auto_tools: bool = False  # エージェント定義としての自動実行(AGT-01d)
    # AGT-MULTI(ADR-0009): SDK選択=ホスト型ReActコンテナのrouting先
    # select_ai = ADB Select AI Agent(DBネイティブ。ENH-04)。他はhosted SDKコンテナ(ADR-0009)
    framework: Literal["openai_agents", "adk", "langgraph", "select_ai"] = "openai_agents"

    def validated(self, owner: str) -> dict:
        return validate_agent_definition(self, owner)


class McpServerCreate(BaseModel):
    label: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=12, max_length=1000)
    auth_token: str | None = Field(default=None, max_length=2000)


class HttpToolCreate(BaseModel):
    """外部HTTPツールの登録(TOOL-01)。

    秘密そのものは受け取らない。Vault に置いた秘密の OCID だけを受け取る
    (`mcp_servers.auth_secret_ocid` と同じ流儀)。
    """

    name: str = Field(min_length=3, max_length=48)
    description: str = Field(min_length=1, max_length=1000)
    parameters: dict = Field(default_factory=lambda: {"type": "object", "properties": {}})
    url: str = Field(min_length=12, max_length=1000)
    method: Literal["GET", "POST"] = "GET"
    auth_header: str | None = Field(default=None, max_length=63)
    auth_secret_ocid: str | None = Field(default=None, max_length=255)
    # TOOL-02: 認証以外に必須ヘッダを持つ相手のための固定ヘッダと、冪等キーのヘッダ名。
    # 値は平文で保存されるので**秘密を入れない**(秘密は auth_secret_ocid = Vault 参照)。
    # 冪等キーの値は登録しない。ヘッダ名だけ登録すれば JetUse が呼び出しごとに発行する
    headers: dict[str, str] | None = Field(default=None)
    idempotency_header: str | None = Field(default=None, max_length=63)


class ToolExecuteRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    arguments: str = Field(default="{}", max_length=10000)
    # TOOL-01: 承認イベントが返した外部HTTPツールの id。指定時はこの id で解決する
    # (名前だけだと承認待ちの間に同名で別 URL のツールへ差し替えられる)
    http_tool_id: str | None = Field(default=None, max_length=36)


class ChartSuggestRequest(BaseModel):
    question: str = Field(default="", max_length=2000)
    columns: list[str] = Field(min_length=1, max_length=50)
    rows: list[list[str]] = Field(default_factory=list, max_length=20)


class PresetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)


class ExtractUrlRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2000)


class UsecaseField(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$")
    label: str = Field(min_length=1, max_length=100)
    type: Literal["text", "textarea", "select", "number", "url"] = "text"
    required: bool = False
    placeholder: str | None = Field(default=None, max_length=300)
    options: list[str] | None = None
    default: str | None = Field(default=None, max_length=300)


class UsecaseDefinition(BaseModel):
    """ユースケース定義(UC-01)。これがDBのdefinition(JSON)の正"""

    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    icon: str | None = Field(default=None, max_length=16)
    tags: list[str] = Field(default_factory=list, max_length=10)
    model: str | None = None
    visibility: Literal["private", "public"] = "private"
    fields: list[UsecaseField] = Field(min_length=1, max_length=20)
    template: str = Field(min_length=1, max_length=20000)

    def validated(self) -> dict:
        return validate_usecase_definition(self)
