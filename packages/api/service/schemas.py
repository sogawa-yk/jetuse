"""リクエスト/レスポンスDTO(Pydantic)。

service/main.py から分離(P1c)。route schema と service層 validator の両方から
import される。`validated()` は service/validators.py 側の純粋関数へ委譲し、
ここでは薄いメソッドとして残す(後方互換 — main.py からの import を維持)。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from jetuse_core import http_tools, rag_metadata, settings, tts, video_search

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
    # RAG-03/ENH-05/RAGM-02(adb=Oracle AI Database 自前索引・チャンク単位の出典)
    rag_backend: Literal["vector_store", "select_ai", "opensearch", "adb"] = "vector_store"
    # RAGM-01: file_searchのメタデータ絞り込み(例 {"type":"eq","key":"current_version",
    # "value":"Y"} で旧版を検索から外す)。vector_storeバックエンドのみ。
    rag_filters: dict | None = None
    # エージェントモード(AGT-01)。tool_resultsは承認フローの継続時に使用
    agent: bool = False
    # AGT-04: エージェントの文書検索(rag_search)のバックエンド。既定は現行と同じ
    # file_search built-in(出典はファイル単位)。adb はチャンク単位の出典
    # (シート名・セル範囲)を返す。`rag=true` との併用禁止は据え置き(別タスク)
    agent_rag_backend: Literal["vector_store", "adb"] = "vector_store"
    # AGT-04: このターンのツール往復上限。未指定は設定値(AGENT_MAX_TOOL_HOPS)。
    # 天井を超える値は 422(クランプしない — ADR-0025)
    # bool は int の派生なので、素の int だと JSON の `true` が 1 として通る。
    # 上限の指定に真偽値が来るのは誤りなので API 境界で断る(resolve_max_tool_hops の
    # bool 拒否と挙動を揃える — 片方だけ厳しいと、どちらが正か読めなくなる)
    max_tool_hops: StrictInt | None = Field(
        default=None, ge=1, le=settings.AGENT_MAX_TOOL_HOPS_CEILING
    )
    auto_tools: bool = False
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
    )
    enabled_tools: list[str] | None = Field(default=None, max_length=20)  # AGT-01b
    mcp_server_ids: list[str] | None = Field(default=None, max_length=5)  # AGT-02
    # TOOL-01: 登録済み外部HTTPツールのid。1エージェントに渡せる数はモデルの選択精度の
    # ためMAX_TOOLS_PER_AGENTで頭打ちにする
    http_tool_ids: list[str] | None = Field(
        default=None, max_length=http_tools.MAX_TOOLS_PER_AGENT
    )
    agent_id: str | None = None  # AGT-03: エージェント定義の適用
    # 画像入力(MM-01): data URI。最終userメッセージに適用(当該ターンのみ・永続化なし)
    # 上限10枚=映像分析のフレーム数を許容(チャットUIは4枚に制限)
    images: list[str] | None = Field(default=None, max_length=10)
    # 監査の機能ラベル(SEC-02。例: usecase:<id> / video / voicechat)
    source: str | None = Field(default=None, max_length=80, pattern=r"^[a-zA-Z0-9:_-]+$")
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


class BuilderPlanPatch(BaseModel):
    """プランの title/description のみ直接編集(SP3-05 / specs/19 §7②)。

    プラン JSON の自由編集はさせない(§11) — extra=forbid で他フィールドは 422。
    上限は DemoPlan(§3.3)と同一。省略 = 変更しない。
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, min_length=1, max_length=1000)


class BuilderGenerateIn(BaseModel):
    """生成開始 body(SP3-06 / specs/19 §4.5)。model = 生成レジストリ(gen_models)の key。

    省略(または body なし)= 設定既定(generation_model)。fail-closed: 未知フィールドは
    422(extra=forbid)。未知 model キーはルート側で生成レジストリと突き合わせて 422。
    """

    model_config = ConfigDict(extra="forbid")

    model: str | None = None


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


class VideoSceneEdit(BaseModel):  # VID-05 (specs/20 §5)
    """場面メタデータの修正。**送られた項目だけ**を直す(`exclude_unset`)。

    `extra="forbid"` にするのは、直せない項目(objects / indoor など)を送ったときに
    **黙って捨てない**ため。捨てると「直したのに変わらない」が起きて理由が判らない。
    値そのものの検証(空文字・列幅・タグの型)は `jetuse_core.video_edit.normalize_edits`
    が持つ —— HTTP とサービス層で二重に持つと、片方だけ直したときに食い違う。
    """

    model_config = ConfigDict(extra="forbid")

    description: str | None = None
    tags: list[str] | None = None
    screen_text: str | None = None
    place: str | None = None
    scene_kind: str | None = None


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


class VideoSearchFilters(BaseModel):
    """場面の絞り込み条件(VID-04 / specs/20 §4)。

    **未知のキーを黙って捨てない**(`extra="forbid"`)。誤字が静かに「条件なしの全件」に
    なると、利用者は絞り込めたつもりで別のものを見る(`jetuse_core.video_search` の
    `SearchInputError` と同じ考えを、ワイヤの入口にも置く)。

    **空文字は「指定なし」に寄せる。** 画面のフォームは未入力の欄を空文字で送るので、
    ここで落とすと未入力の欄がひとつでもあるだけで検索が 422 になる。
    """

    model_config = ConfigDict(extra="forbid")

    # 期間。日付だけ("2026-12-31")ならその日を丸ごと含める(video_search._time_bound)
    captured_from: str | None = Field(default=None, max_length=64)
    captured_to: str | None = Field(default=None, max_length=64)
    created_from: str | None = Field(default=None, max_length=64)
    created_to: str | None = Field(default=None, max_length=64)
    collection: str | None = Field(default=None, max_length=video_search.VALUE_MAX)
    category: str | None = Field(default=None, max_length=video_search.VALUE_MAX)
    rights: str | None = Field(default=None, max_length=video_search.VALUE_MAX)
    place: str | None = Field(default=None, max_length=video_search.VALUE_MAX)
    # 集合が決まっている項目も**型は str にする**。許される値は DB の CHECK 制約と
    # 同じ集合(`video_search._ENUM_FILTERS`)が単一の真実源で、ここに写すと 2 か所を
    # 揃え続けることになる。集合外の値は core が許容値つきの 422 で返す
    indoor: str | None = Field(default=None, max_length=video_search.VALUE_MAX)
    time_of_day: str | None = Field(default=None, max_length=video_search.VALUE_MAX)
    has_people: bool | None = None
    tags: list[str] | None = Field(default=None, max_length=video_search.TAGS_MAX)
    duration_min_ms: StrictInt | None = Field(default=None, ge=0)
    duration_max_ms: StrictInt | None = Field(default=None, ge=0)
    analysis_state: str | None = Field(default=None, max_length=video_search.VALUE_MAX)
    confirmed: bool | None = None

    @field_validator("*", mode="before")
    @classmethod
    def _blank_is_unset(cls, value):
        if isinstance(value, str) and not value.strip():
            return None
        return value


class VideoUploadUrlRequest(BaseModel):
    """`POST /api/video/assets/upload-url`(VID-07 / specs/20 §2)。

    **本体は載らない。** ここで受け取るのは「これから何を上げるか」の申告だけで、
    映像そのものはブラウザが Object Storage へ直接 PUT する(ゲートウェイの本文上限
    20 MiB を通さないため)。`size_bytes` は上限超過を**発行前に**弾くために要る ——
    500MB を超える映像に PAR を配ってから落とすと、上げ切った後で失敗を知らせることになる。

    **未知のキーを弾く**(`extra="forbid"`)。`{"filename": ..., "titel": ...}` のような
    誤字を無視すると、題名が付かないまま登録が成功する。
    """

    model_config = ConfigDict(extra="forbid")

    filename: str = Field(min_length=1, max_length=1000)
    # **`StrictInt`。** 素の int は `"100"` や `true` を数として通す。サイズは
    # 文字列でも真偽値でもない(上限判定が静かにずれる)
    size_bytes: StrictInt = Field(ge=1)
    title: str | None = Field(default=None, max_length=500)
    collection: str | None = Field(default=None, max_length=255)
    category: str | None = Field(default=None, max_length=255)
    rights: str | None = Field(default=None, max_length=1000)
    # 撮影日時は multipart 経路と同じ規則(ISO-8601 / 読めなければ 422)で
    # ルータ側が解釈する。**読めない値を黙って NULL にしない**
    captured_at: str | None = Field(default=None, max_length=64)
    duration_ms: StrictInt | None = Field(default=None, ge=0)

    @field_validator("*", mode="before")
    @classmethod
    def _blank_is_unset(cls, value):
        if isinstance(value, str) and not value.strip():
            return None
        return value


class VideoSearchRequest(BaseModel):
    """`POST /api/video/search`(VID-04 / specs/20 §4)。

    `q` と `similar_to_scene_id` の同時指定は `video_search.search` が 422 で弾く
    (2 つのベクトルの混ぜ方を仕様が決めていない。勝手に決めて片方を捨てない)。

    **未知のキーは filters と同じくここでも弾く**(`extra="forbid"`)。`{"query": "豪雨"}`
    のような誤字を無視すると、検索語なしの一覧要求として成功し、**利用者が検索した
    つもりで全場面を見る**(filters だけ塞いでもトップレベルに同じ穴が残る)。
    """

    model_config = ConfigDict(extra="forbid")

    q: str | None = Field(default=None, max_length=1000)
    filters: VideoSearchFilters | None = None
    similar_to_scene_id: str | None = Field(default=None, max_length=64)
    # **`StrictInt`。** 素の `int` は lax モードで `true` を 1 に、`"20"` を 20 に
    # 変換するので、`{"limit": true}` が「1 件」として通ってしまう(core の
    # `_limit_value` と規則が食い違う)。件数は文字列でも真偽値でもない。
    limit: StrictInt = Field(
        default=video_search.DEFAULT_LIMIT, ge=1, le=video_search.LIMIT_MAX
    )
