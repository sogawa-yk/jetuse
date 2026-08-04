"""SP1-01: 能力カタログ(GET /api/capabilities)のテスト。

カタログの正しさの要 = ディスクリプタの routes が app.openapi() に実在すること
(ディスクリプタとルートの乖離をここで検出する)。
"""

import typing

import pytest
from fastapi.testclient import TestClient

from jetuse_core.capabilities import (
    CAPABILITIES,
    RAG_BACKEND_AXES,
    RAG_SUPPORT_LEVELS,
)
from jetuse_core.settings import get_settings
from service.main import app
from service.schemas import ChatRequest

client = TestClient(app)

# specs/17 §4 の「デモ向け能力」8件
EXPECTED_CAPABILITIES = {
    "chat", "rag.search", "dbchat", "agents",
    "voice", "minutes", "translate", "docunderstand",
}


@pytest.fixture(autouse=True)
def reset_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_returns_eight_capabilities_matching_spec():
    res = client.get("/api/capabilities")
    assert res.status_code == 200
    caps = res.json()["capabilities"]
    assert len(caps) == 8
    assert {c["capability"] for c in caps} == EXPECTED_CAPABILITIES
    for c in caps:
        assert c["demo_safe"] is True
        assert c["summary"]
        assert c["when_to_use"]
        assert isinstance(c["example"], dict)
        assert c["routes"]


def test_descriptor_routes_exist_in_openapi():
    spec_paths = app.openapi()["paths"]
    for cap in CAPABILITIES:
        for route in cap["routes"]:
            assert route["path"] in spec_paths, (
                f"{cap['capability']}: {route['path']} not in openapi paths"
            )
            assert route["method"] in spec_paths[route["path"]], (
                f"{cap['capability']}: {route['method']} {route['path']}"
            )


def test_response_attaches_openapi_fragments():
    res = client.get("/api/capabilities")
    for c in res.json()["capabilities"]:
        for route in c["routes"]:
            frag = c["openapi"][route["path"]][route["method"]]
            assert "responses" in frag


def test_backstage_routes_not_in_catalog():
    # specs/17 §4: admin / conversations / tools / mcp_servers 等の裏方は載せない
    backstage = ("/api/admin", "/api/conversations", "/api/tools",
                 "/api/agent/mcp-servers", "/api/db/datasets", "/api/usecases")
    for cap in CAPABILITIES:
        for route in cap["routes"]:
            assert not route["path"].startswith(backstage), route["path"]


def test_example_model_keys_are_public_registry_keys():
    # example の model は公開レジストリのキー(SP1-01-001: 内部IDを書くと400になる)
    from jetuse_core.models import MODELS

    for cap in CAPABILITIES:
        model = cap["example"].get("input", {}).get("model")
        if model is not None:
            assert model in MODELS, f"{cap['capability']}: {model}"


# --- RAGM-03: RAG バックエンドの能力差(ADR-0020 §3) ---------------------------


def _rag_backend_capabilities() -> dict:
    res = client.get("/api/capabilities")
    cap = next(c for c in res.json()["capabilities"] if c["capability"] == "rag.search")
    return cap["backend_capabilities"]


def test_rag_backends_match_the_api_contract():
    # 選べるバックエンド(ChatRequest.rag_backend)と能力表がズレたら、画面は
    # 「選べるのに何が使えるか分からない」バックエンドを出してしまう。
    selectable = set(typing.get_args(ChatRequest.model_fields["rag_backend"].annotation))
    assert set(_rag_backend_capabilities()["backends"]) == selectable


def test_every_backend_describes_every_axis():
    backends = _rag_backend_capabilities()["backends"]
    for name, be in backends.items():
        assert be["label"] and be["role"], name
        assert set(be["axes"]) == set(RAG_BACKEND_AXES), name
        for axis, entry in be["axes"].items():
            assert entry["support"] in RAG_SUPPORT_LEVELS, f"{name}.{axis}"
            assert entry["detail"] and entry["evidence"], f"{name}.{axis}"


def test_unverified_capabilities_are_never_claimed_as_available():
    # このタスクの核。実機で確かめていないものを「できる」と書かせない。
    for name, be in _rag_backend_capabilities()["backends"].items():
        for axis, entry in be["axes"].items():
            where = f"{name}.{axis}"
            if entry["support"] in ("yes", "limited"):
                assert entry["verified"] is True, where
            if entry["support"] == "unverified":
                assert entry["verified"] is False, where


# SPIKE-M1 が実行結果を残していない項目(runs/2026-07-28T1848_SPIKE-M1/e2e/SKIPPED.md 3)。
# ここを "yes" にも "limited" にもできないよう期待値で固定する。"limited" にすると画面には
# 「条件付きで使える」と出る = 未実証の能力を顧客に見せることになる。
UNPROVEN_AXES = {
    ("adb", "row_level_security"),        # VPD がベクタ検索に効くこと
    ("adb", "business_data_join"),        # 業務表と JOIN したベクタ検索
    ("select_ai", "row_level_security"),
}


# 支持レベルの期待値を固定する(RAGM03-001)。「表の1行だけを見た人が誤解しないか」を
# 支持レベルで担保する — detail に但し書きがあるからと yes に上げてよいことにしない。
EXPECTED_SUPPORT = {
    # チャット API から条件を渡す口は vector_store にしか無い。ADB は常に現行版のみ。
    ("adb", "filter_expressiveness"): "limited",
    ("vector_store", "filter_expressiveness"): "limited",
    # ADB が実測で優位なのはこの 2 つ(チャンク単位の出典・同一トランザクション)。
    ("adb", "citation_granularity"): "yes",
    ("adb", "metadata_update_consistency"): "yes",
}


@pytest.mark.parametrize(("key", "expected"), sorted(EXPECTED_SUPPORT.items()))
def test_support_levels_match_what_is_actually_reachable(key: tuple[str, str], expected: str):
    backend, axis = key
    entry = _rag_backend_capabilities()["backends"][backend]["axes"][axis]
    assert entry["support"] == expected, f"{backend}.{axis}"


@pytest.mark.parametrize(("backend", "axis"), sorted(UNPROVEN_AXES))
def test_unproven_axes_stay_unverified(backend: str, axis: str):
    entry = _rag_backend_capabilities()["backends"][backend]["axes"][axis]
    assert entry["support"] == "unverified", f"{backend}.{axis}"
    assert entry["verified"] is False


def test_row_level_security_is_never_advertised_as_available():
    # VPD はベクタ検索に効くことが未実証。どのバックエンドでも「使える」と出してはいけない。
    for name, be in _rag_backend_capabilities()["backends"].items():
        assert be["axes"]["row_level_security"]["support"] in ("no", "unverified"), name


def test_requires_auth(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    get_settings.cache_clear()
    res = client.get("/api/capabilities")
    assert res.status_code == 401
