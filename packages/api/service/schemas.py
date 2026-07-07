"""リクエスト/レスポンスDTO(Pydantic)。

service/main.py から分離(P1c)。route schema と service層 validator の両方から
import される。`validated()` は service/validators.py 側の純粋関数へ委譲し、
ここでは薄いメソッドとして残す(後方互換 — main.py からの import を維持)。
"""

from typing import Literal

from pydantic import BaseModel, Field

from jetuse_core import tts

from .validators import validate_agent_definition, validate_usecase_definition


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    temperature: float | None = Field(default=None, ge=0, le=2)
    # 生成パラメータ拡張(CHAT-04b)。未指定はAPIに渡さない=モデル既定
    top_p: float | None = Field(default=None, gt=0, le=1)
    max_tokens: int | None = Field(default=None, ge=1, le=32768)
    reasoning_effort: Literal["low", "medium", "high"] | None = None
    conversation_id: str | None = None  # 指定時はADBへ永続化(CHAT-02)
    persist_user: bool = True  # 再生成時はfalse(ユーザー発話の二重保存防止)
    rag: bool = False  # file_searchツール接続(RAG-02。Responses系のみ)
    # RAG-03/ENH-05
    rag_backend: Literal["vector_store", "select_ai", "opensearch"] = "vector_store"
    # エージェントモード(AGT-01)。tool_resultsは承認フローの継続時に使用
    agent: bool = False
    auto_tools: bool = False
    tool_results: list[dict] | None = Field(default=None, max_length=24)
    enabled_tools: list[str] | None = Field(default=None, max_length=20)  # AGT-01b
    mcp_server_ids: list[str] | None = Field(default=None, max_length=5)  # AGT-02
    agent_id: str | None = None  # AGT-03: エージェント定義の適用
    # 画像入力(MM-01): data URI。最終userメッセージに適用(当該ターンのみ・永続化なし)
    # 上限10枚=映像分析のフレーム数を許容(チャットUIは4枚に制限)
    images: list[str] | None = Field(default=None, max_length=10)
    # 監査の機能ラベル(SEC-02。例: usecase:<id> / video / voicechat)
    source: str | None = Field(default=None, max_length=80, pattern=r"^[a-zA-Z0-9:_-]+$")
    # Agents SDK承認往復(FW-01b): 中断時のsdk_stateを返送し、call_id→可否を添える
    sdk_state: str | None = Field(default=None, max_length=2_000_000)
    sdk_approvals: dict[str, bool] | None = None


class ConversationCreate(BaseModel):
    model: str
    title: str | None = None


class DemoCreate(BaseModel):
    """Demo 作成(SP2-01 / specs/18 §2.2)。config の 1MB/dbchat 形状はルート側の共通検証。"""

    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    visibility: Literal["private", "public"] = "private"
    config: dict = Field(default_factory=dict)


class DemoPatch(BaseModel):
    """Demo 部分更新(specs/18 §2.2)。省略 = 変更しない(exclude_unset)。明示 null は
    description のみ許可(クリア)。id/owner_sub/status は変更不可(入力スキーマに含めない)。"""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    visibility: Literal["private", "public"] | None = None
    config: dict | None = None


class BuilderMessageIn(BaseModel):
    """ヒアリング発話(SP3-01 / specs/19 §2.1 — 発話 1 件 ≤ 4,000 文字。超過は 422)。"""

    content: str = Field(min_length=1, max_length=4000)


class Nl2SqlRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    backend: Literal["sql_search", "select_ai"] = "sql_search"  # SQL-04比較モード
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


class ToolExecuteRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    arguments: str = Field(default="{}", max_length=10000)


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
