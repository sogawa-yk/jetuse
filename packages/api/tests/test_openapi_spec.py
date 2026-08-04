"""API-01: OpenAPI 仕様の外部公開。

固定したいのは3つ。
1. 仕様が HTTP で取れて OpenAPI として parse できる(= 生成器に渡せる)。
2. 能力登録簿(`/api/capabilities`)と乖離していない = 登録簿の全 route が仕様に実在する。
   登録簿は仕様から断片を導出しており、こちらが**ワイヤ契約の正本**。
3. 実際に人が詰まった A/B/C の関係が**説明文から読み取れる**(排他: agent×rag /
   依存: 文書検索は enabled_tools に rag_search / 似た名前: rag_backend 対 agent_rag_backend)。
"""

import json

import pytest
from fastapi.testclient import TestClient

from jetuse_core.capabilities import CAPABILITIES
from jetuse_core.settings import get_settings
from service.main import app

client = TestClient(app)

# Gateway のキャッチオール `/api/{p*}` に乗る唯一の公開先(Terraform 変更なしで到達できる)。
SPEC_URL = "/api/openapi.json"

_OPERATION_KEYS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
_PATH_ITEM_KEYS = _OPERATION_KEYS | {"$ref", "summary", "description", "servers", "parameters"}


@pytest.fixture(autouse=True)
def reset_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _spec() -> dict:
    res = client.get(SPEC_URL)
    assert res.status_code == 200, res.text
    assert res.headers["content-type"].startswith("application/json")
    return res.json()


def _chat_request_properties() -> dict:
    return _spec()["components"]["schemas"]["ChatRequest"]["properties"]


def _chat_stream_description() -> str:
    return _spec()["paths"]["/api/chat/stream"]["post"]["description"]


def test_spec_is_served_over_http_and_parses_as_openapi():
    spec = _spec()
    assert spec["openapi"].startswith("3."), spec["openapi"]
    assert spec["info"]["title"]
    assert spec["info"]["version"]
    assert spec["paths"]
    for path, item in spec["paths"].items():
        assert path.startswith("/"), path
        assert set(item) <= _PATH_ITEM_KEYS, path
        for method, op in item.items():
            if method in _OPERATION_KEYS:
                assert "responses" in op, f"{method} {path}"
    # 生成器へ渡せる形であること(JSON として往復できる)
    assert json.loads(json.dumps(spec)) == spec


def test_the_spec_endpoint_is_in_the_spec_itself():
    # 仕様の入口が仕様から分かること(取り方を docs でしか知れない状態にしない)
    assert "get" in _spec()["paths"][SPEC_URL]


def test_spec_is_served_only_from_the_api_prefix():
    # FastAPI 既定の口は塞ぐ。仕様を返す経路を1本に限ることで、認証が要る配備で
    # **仕様だけ無認証で晒す経路が残らない**(fail-closed)。
    assert app.openapi_url is None
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404


def test_spec_requires_auth_when_auth_is_required(monkeypatch):
    # `auth_required=true` の配備で仕様だけ無認証で取れてはいけない(API-01 の禁止事項)。
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    get_settings.cache_clear()
    assert client.get(SPEC_URL).status_code == 401


def test_capability_registry_routes_all_exist_in_the_served_spec():
    # 登録簿と仕様のズレ検出。HTTP で配った仕様に対して検査する(in-process の
    # app.openapi() では、公開経路が壊れていてもこの検査だけ通ってしまう)。
    paths = _spec()["paths"]
    for cap in CAPABILITIES:
        for route in cap["routes"]:
            assert route["path"] in paths, f"{cap['capability']}: {route['path']}"
            assert route["method"] in paths[route["path"]], (
                f"{cap['capability']}: {route['method']} {route['path']}"
            )


def test_spec_says_agent_and_rag_are_exclusive():
    props = _chat_request_properties()
    for field in ("agent", "rag"):
        desc = props[field]["description"]
        assert "併用できない" in desc, field
        assert "agent and rag cannot be combined" in desc, field
    assert "agent and rag cannot be combined" in _chat_stream_description()


def test_spec_says_agent_doc_search_needs_rag_search_in_enabled_tools():
    props = _chat_request_properties()
    assert "rag_search" in props["enabled_tools"]["description"]
    assert "rag_search" in props["agent"]["description"]
    adb_desc = props["agent_rag_backend"]["description"]
    assert "agent_rag_backend requires rag_search in enabled_tools" in adb_desc
    # 400 になるのは既定でない値(`adb`)のときだけ。無条件に書くと仕様が実挙動より厳しくなる
    # (review-1 F-002)。境界の挙動は tests/test_agent_hops_and_adb_rag.py が固定している。
    assert "`adb` を選ぶときだけ" in adb_desc
    assert "`vector_store` は明示しても検査されない" in adb_desc
    assert "rag_search" in _chat_stream_description()


def test_spec_distinguishes_rag_backend_from_agent_rag_backend():
    props = _chat_request_properties()
    assert "agent_rag_backend" in props["rag_backend"]["description"]
    assert "rag_backend" in props["agent_rag_backend"]["description"]
    assert "agent_rag_backend" in _chat_stream_description()


# --- エラー応答（コーディングエージェントが自己修正できるための最低条件） ---------------

_SSE_ROUTES = (
    ("/api/chat/stream", "post"),
    ("/api/chat/ping", "get"),
    ("/api/chat/nl2sql", "post"),
    ("/api/minutes/{mid}/generate", "post"),
    ("/api/stt/sessions/{sid}/events", "get"),
    ("/api/demos/{demo_id}/chat", "post"),
)

_ERROR_ROUTES = {
    # 実装が返しうるコードを漏らさない。漏らすと `raise_on_unexpected_status=True` の
    # 生成クライアントが障害時に型のない例外で落ちる（review-7 F-001/F-002）。
    ("/api/chat/stream", "post"): (400, 404, 413, 422, 503),
    ("/api/rag/files", "post"): (413, 422, 502, 503),
    ("/api/agent/execute-tool", "post"): (400, 404, 409, 422, 503),
    ("/api/minutes/{mid}/generate", "post"): (400, 404, 409, 503),
}


@pytest.mark.parametrize(("path", "method"), _SSE_ROUTES)
def test_sse_routes_declare_the_event_stream_media_type(path: str, method: str):
    """SSE を返すルートが `application/json` を宣言していると、生成クライアントは
    ストリームを JSON としてデコードしようとする（review-9 F-001）。"""
    content = _spec()["paths"][path][method]["responses"]["200"]["content"]
    assert list(content) == ["text/event-stream"], f"{method} {path}: {list(content)}"


def test_sse_response_really_is_event_stream(monkeypatch):
    """宣言と実 Content-Type が一致すること（宣言だけ直しても意味が無い）。"""
    import service.main as service_main

    def one_delta(*a, **kw):
        yield {"delta": "ok"}

    monkeypatch.setattr(service_main, "stream_chat", one_delta)
    res = client.post("/api/chat/stream", json={
        "model": "gpt-oss-120b", "messages": [{"role": "user", "content": "x"}]})
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")
    assert res.text.rstrip().endswith("data: [DONE]")


def test_authenticated_routes_declare_401():
    """`AUTH_REQUIRED=true` の配備で必ず起きる 401 を宣言し忘れない（review-9 F-002）。"""
    spec = _spec()
    # `/healthz` だけは意図的に認証なし（配備の生存確認。ここに 401 が要る配備は運用できない）
    unauthenticated = {"/healthz"}
    checked = 0
    for path, item in spec["paths"].items():
        for method, op in item.items():
            if method not in _OPERATION_KEYS or path in unauthenticated:
                continue
            responses = op["responses"]
            assert "401" in responses, f"{method} {path}"
            ref = responses["401"]["content"]["application/json"]["schema"]["$ref"]
            assert ref.endswith("ErrorResponse"), f"{method} {path}"
            checked += 1
    assert checked > 50  # 全ルートを見ている


def test_declared_401_matches_the_real_unauthenticated_response(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    get_settings.cache_clear()
    for path in (SPEC_URL, "/api/capabilities", "/api/rag/files"):
        res = client.get(path)
        assert res.status_code == 401, path
        assert isinstance(res.json()["detail"], str), path


@pytest.mark.parametrize(("route", "codes"), sorted(_ERROR_ROUTES.items()))
def test_error_codes_are_documented_with_meaning_and_shape(route: tuple, codes: tuple):
    path, method = route
    responses = _spec()["paths"][path][method]["responses"]
    for code in codes:
        entry = responses[str(code)]
        # 「どういうときに返るか」が説明にある（コードだけ並べても直し方が分からない）
        assert len(entry["description"]) > 40, f"{method} {path} {code}"
        schema = entry["content"]["application/json"]["schema"]
        if code == 422:
            # 422 は**実装が 2 通り返す**（スキーマ検証=配列 / ルート側の検証=文字列）。
            # 片方だけ載せると生成クライアントが実応答を解けない（review-6 F-001）。
            refs = {alt["$ref"].rsplit("/", 1)[-1] for alt in schema["oneOf"]}
            assert refs == {"HTTPValidationError", "ErrorResponse"}, f"{method} {path}"
        else:
            assert schema["$ref"].endswith("ErrorResponse"), f"{method} {path} {code}"


def test_both_422_shapes_actually_occur(monkeypatch):
    """仕様が 422 を 2 形で宣言しているのは**実装がそう返すから**。実際に両方出す。

    片方しか起きないなら宣言が過剰、両方起きるのに片方しか宣言していなければ嘘になる。
    """
    import service.main as service_main

    # (a) スキーマ検証で落ちる → detail は配列
    res = client.post("/api/chat/stream", json={"messages": [{"role": "user", "content": "x"}]})
    assert res.status_code == 422
    assert isinstance(res.json()["detail"], list)

    # (b) ルート側の検証で落ちる → detail は文字列
    monkeypatch.setattr(service_main, "stream_chat", lambda *a, **kw: iter(()))
    res = client.post("/api/chat/stream", json={
        "model": "gpt-oss-120b", "messages": [{"role": "user", "content": "x"}],
        "agent": True, "images": ["data:image/png;base64,AAAA"]})
    assert res.status_code == 422
    assert isinstance(res.json()["detail"], str)

    # (c) アップロード側も文字列（拡張子の拒否。保存は起きない）
    res = client.post(
        "/api/rag/files", files={"file": ("x.exe", b"MZ", "application/octet-stream")}
    )
    assert res.status_code == 422
    assert isinstance(res.json()["detail"], str)


def test_declared_503_matches_the_db_outage_response(monkeypatch):
    """DB 障害の 503 は共通ハンドラが返す。**仕様に書いたコードが実際に出る**ことを見る。

    ルートごとに `responses` を書いても、実装が返すコードを漏らしていれば
    生成クライアントは障害時に解けない（review-7 F-002）。
    """
    import oracledb

    import service.main as service_main

    def boom(*a, **kw):
        raise oracledb.OperationalError("DPY-6005: cannot connect to database")

    monkeypatch.setattr(service_main.conv_repo, "get_conversation", boom)
    res = client.post("/api/chat/stream", json={
        "model": "gpt-oss-120b", "conversation_id": "c1",
        "messages": [{"role": "user", "content": "q"}]})
    assert res.status_code == 503
    assert res.json() == {"detail": "database unavailable"}
    assert "503" in _spec()["paths"]["/api/chat/stream"]["post"]["responses"]

    monkeypatch.setattr(service_main.http_tools_repo, "get_tools", boom)
    res = client.post("/api/agent/execute-tool",
                      json={"name": "unknown_tool", "http_tool_id": "t1"})
    assert res.status_code == 503
    assert "503" in _spec()["paths"]["/api/agent/execute-tool"]["post"]["responses"]

    monkeypatch.setattr(service_main.minutes_repo, "get_job", boom)
    res = client.post("/api/minutes/m1/generate", json={"model": "gpt-oss-120b"})
    assert res.status_code == 503
    assert "503" in _spec()["paths"]["/api/minutes/{mid}/generate"]["post"]["responses"]


def test_declared_502_matches_the_upstream_failure_response(monkeypatch):
    """OCI GenAI が 4xx/5xx を返したときの 502（レート制限・障害）。宣言と実応答を合わせる。"""
    import httpx
    from openai import APIStatusError

    import service.routes.rag as rag_route

    def upstream_429(*a, **kw):
        raise APIStatusError(
            "rate limited",
            response=httpx.Response(429, request=httpx.Request("POST", "https://example.test")),
            body=None,
        )

    monkeypatch.setattr(rag_route.rag, "add_file", upstream_429)
    res = client.post("/api/rag/files", files={"file": ("a.txt", b"hello", "text/plain")})
    assert res.status_code == 502
    assert isinstance(res.json()["detail"], str)
    assert "502" in _spec()["paths"]["/api/rag/files"]["post"]["responses"]


def test_error_shapes_are_defined_as_schemas():
    schemas = _spec()["components"]["schemas"]
    # 422 以外の共通形
    detail = schemas["ErrorResponse"]["properties"]["detail"]
    assert detail["type"] == "string"
    assert detail["description"]
    assert detail.get("examples")
    # 422 のもう一方の形（loc / msg / type の配列）
    validation = schemas["HTTPValidationError"]["properties"]["detail"]
    assert validation["type"] == "array"
    item = schemas["ValidationError"]["properties"]
    assert {"loc", "msg", "type"} <= set(item)


def test_spec_says_where_tokens_come_from():
    """トークンの取り方が無いと、エージェントは発行エンドポイントを**でっち上げる**。

    実測（`runs/.../e2e/scenario-4-agent-with-spec-only.md`）で、仕様だけを渡した 2 体とも
    「`security: HTTPBearer` はあるが取得方法が仕様に無い」と推測に回していた。
    """
    description = _spec()["info"]["description"]
    assert "トークンを発行する口は無い" in description
    assert "AUTH_REQUIRED" in description


def test_spec_explains_the_error_contract_up_front():
    # ルートごとの responses だけでは「まず何を見ればいいか」が分からないので、
    # コードの意味と 422 の 2 形を info.description にも書く。
    description = _spec()["info"]["description"]
    # 一覧に載せるコードは openapi_errors._MEANINGS と揃える（片方だけ増えると嘘になる）
    from service.openapi_errors import _MEANINGS
    for code in _MEANINGS:
        assert f"`{code}`" in description, code
    for token in ('{"detail"', "loc", "両方を受けられるように"):
        assert token in description, token
    # SSE は HTTP 200 のまま本文でエラーになりうる（エージェントが取りこぼす典型）
    assert "error" in description and "[DONE]" in description


# --- 例（エージェントは例から形を学ぶ。**間違った例は害になる**ので実際に受理されるものだけ） ---


def _named_examples() -> dict[str, dict]:
    """名前つきの例は requestBody 側（OpenAPI Example Object）。"""
    body = _spec()["paths"]["/api/chat/stream"]["post"]["requestBody"]
    return body["content"]["application/json"]["examples"]


def _example_values() -> list[dict]:
    """schema 側の `examples` は**生のリクエスト値**の配列（review-6 F-003）。"""
    return _spec()["components"]["schemas"]["ChatRequest"]["examples"]


def test_schema_examples_are_raw_request_values():
    values = _example_values()
    assert values
    for v in values:
        # Example Object を schema に置くと利用者には summary/value というリクエストに見える
        assert not ({"summary", "value"} & set(v)), v
        assert "model" in v and "messages" in v


def test_chat_request_has_examples_including_agent_document_search():
    named = _named_examples()
    assert len(named) >= 2
    for ex in named.values():
        assert ex["summary"] and ex["description"] and ex["value"]
    # schema 側の生の値と requestBody 側の値が一致する（正本が 1 つ）
    assert [ex["value"] for ex in named.values()] == _example_values()
    values = _example_values()
    # エージェント実行の例（文書検索の正しい形）が要る
    agent_ex = [v for v in values if v.get("agent")]
    assert agent_ex, "エージェント実行の例が無い"
    assert any("rag_search" in (v.get("enabled_tools") or []) for v in agent_ex)
    # rag（文書 Q&A）の例も要る
    assert any(v.get("rag") for v in values)


def test_no_example_violates_the_documented_exclusivity():
    # 例が排他を破っていたら、エージェントは 400 になる形を学んでしまう。
    for value in _example_values():
        assert not (value.get("rag") and (value.get("agent") or value.get("agent_id")))


def test_examples_are_actually_accepted_by_the_route(monkeypatch):
    """例が**実ルートで受理される**ことまで見る（仕様と実装のズレは例にも出る）。

    LLM 呼び出しだけ差し替え、検証（400/422 を出す層）は本物を通す。
    `rag=true` の例は Vector Store の実体が要るので、ここでは検証層の通過だけ確認する
    （実環境での成功は E2E 側で示す）。
    """
    import service.main as service_main

    def one_delta(*args, **kw):
        yield {"delta": "ok"}

    monkeypatch.setattr(service_main, "stream_chat", one_delta)
    monkeypatch.setattr(service_main, "stream_agent", one_delta)
    monkeypatch.setattr(
        service_main.rag, "get_store_id", lambda *a, **kw: "vs_stub"
    )
    for name, ex in _named_examples().items():
        res = client.post("/api/chat/stream", json=ex["value"])
        assert res.status_code == 200, f"{name}: {res.status_code} {res.text[:200]}"


def test_rag_upload_documents_how_to_call_it_with_an_example():
    # multipart は schema の examples に載せられないので、操作の説明に実例を書く。
    description = _spec()["paths"]["/api/rag/files"]["post"]["description"]
    assert "curl" in description
    assert "-F" in description and "file=@" in description
    assert "いつ使うか" in description


# --- 「いつ使うか」（型だけではエージェントが使い分けを推測してしまう） -------------------

_WHEN_TO_USE_FIELDS = (
    "model", "messages", "agent", "rag", "rag_backend", "agent_rag_backend",
    "enabled_tools", "auto_tools", "agent_id", "http_tool_ids", "mcp_server_ids",
    "max_tool_hops", "tool_results", "conversation_id", "images",
)


@pytest.mark.parametrize("field", _WHEN_TO_USE_FIELDS)
def test_main_parameters_say_when_to_use_them(field: str):
    description = _chat_request_properties()[field].get("description", "")
    # 型の言い換えだけの説明を弾く緩い下限（実運用の説明はどれも十分長い）
    assert len(description) > 30, f"{field}: {description!r}"


def test_spec_points_at_the_capability_registry_for_use_cases():
    # 二重管理にしないための取り決め(用途は登録簿・契約は仕様)を仕様本文に書いておく。
    description = _spec()["info"]["description"]
    assert "/api/capabilities" in description
    assert SPEC_URL in description
