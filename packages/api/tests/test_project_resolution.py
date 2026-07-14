"""FIX-47: PROJECT_OCID 解決(検索→自動作成→fail-fast)・ragルートのエラー表面化・/api/rag/health。

Issue #47 根治: DP 状態API(OpenAi-Project ヘッダ必須)へ空ヘッダを送らない。
"""

from types import SimpleNamespace

import httpx
import openai
import pytest
from fastapi.testclient import TestClient

from jetuse_core import genai, rag
from jetuse_core.settings import Settings
from service.main import app

client = TestClient(app)

COMP = "ocid1.compartment.oc1..testcomp"


def _settings(**kw):
    kw.setdefault("compartment_ocid", COMP)
    return Settings(_env_file=None, **kw)


class FakeSdk:
    """GenerativeAiClient の list/create/get だけを再現(oci.pagination 互換)。"""

    def __init__(self, items=None, fail=None, create_state="ACTIVE"):
        self.items = list(items or [])
        self.fail = fail
        self.create_state = create_state
        self.list_calls = 0
        self.created = []

    def list_generative_ai_projects(self, compartment_id, **kw):
        self.list_calls += 1
        if self.fail:
            raise self.fail
        return SimpleNamespace(
            data=SimpleNamespace(items=self.items), has_next_page=False, next_page=None,
            status=200, headers={}, request=None,
        )

    def create_generative_ai_project(self, details):
        if self.fail:
            raise self.fail
        self.created.append(details)
        proj = SimpleNamespace(
            id="ocid1.generativeaiproject.oc1..auto", lifecycle_state=self.create_state
        )
        self.items.append(proj)
        return SimpleNamespace(data=proj)

    def get_generative_ai_project(self, project_id):
        return SimpleNamespace(data=self.items[-1])


@pytest.fixture(autouse=True)
def reset_project_cache():
    genai._reset_project_cache()
    yield
    genai._reset_project_cache()


@pytest.fixture(autouse=True)
def no_real_signer(monkeypatch):
    """CI(GitHub Actions)には ~/.oci/config が無い。署名器はクライアント構築に不要なのでスタブ。"""
    monkeypatch.setattr(genai, "_signer", lambda: None)


# --- resolve_project_ocid ---


def test_env_project_short_circuits(monkeypatch):
    def boom(settings):
        raise AssertionError("SDK must not be called when PROJECT_OCID is set")

    monkeypatch.setattr(genai, "_sdk_client", boom)
    s = _settings(project_ocid="ocid1.generativeaiproject.oc1..env")
    assert genai.resolve_project_ocid(s) == "ocid1.generativeaiproject.oc1..env"


def test_auto_picks_active_and_caches(monkeypatch):
    sdk = FakeSdk(items=[
        SimpleNamespace(id="ocid1.generativeaiproject.oc1..dead", lifecycle_state="DELETED"),
        SimpleNamespace(id="ocid1.generativeaiproject.oc1..live", lifecycle_state="ACTIVE"),
    ])
    monkeypatch.setattr(genai, "_sdk_client", lambda s: sdk)
    s = _settings()
    assert genai.resolve_project_ocid(s) == "ocid1.generativeaiproject.oc1..live"
    assert genai.resolve_project_ocid(s) == "ocid1.generativeaiproject.oc1..live"
    assert sdk.list_calls == 1  # 2回目はプロセス内キャッシュ
    assert sdk.created == []  # 既存があれば作らない


def test_auto_creates_when_none_and_enabled(monkeypatch):
    sdk = FakeSdk(items=[])
    monkeypatch.setattr(genai, "_sdk_client", lambda s: sdk)
    s = _settings(project_autocreate=True)
    assert genai.resolve_project_ocid(s) == "ocid1.generativeaiproject.oc1..auto"
    assert len(sdk.created) == 1
    assert sdk.created[0].compartment_id == COMP


def test_autocreate_disabled_fails_fast(monkeypatch):
    """既定(PROJECT_AUTOCREATE=false)では作成せず actionable に raise(REV-001 blocker#2)。"""
    sdk = FakeSdk(items=[])
    monkeypatch.setattr(genai, "_sdk_client", lambda s: sdk)
    with pytest.raises(genai.ProjectResolutionError) as ei:
        genai.resolve_project_ocid(_settings())
    assert sdk.created == []
    assert "PROJECT_OCID" in str(ei.value)


def test_allow_autocreate_false_suppresses_creation_even_when_enabled(monkeypatch):
    """PORT-02レビュー指摘: /api/health等のGET診断エンドポイントがポーリングだけで
    GenerativeAiProjectを作らないよう、呼び出し側でautocreateを明示的に抑制できる。"""
    sdk = FakeSdk(items=[])
    monkeypatch.setattr(genai, "_sdk_client", lambda s: sdk)
    s = _settings(project_autocreate=True)  # 有効環境でも
    with pytest.raises(genai.ProjectResolutionError):
        genai.resolve_project_ocid(s, allow_autocreate=False)
    assert sdk.created == []  # 作成されない
    assert genai._project_cache is None


def test_nonactive_created_project_not_cached(monkeypatch):
    """作成後 ACTIVE に達しない project は使わずキャッシュもしない(REV-001 major#2)。"""
    sdk = FakeSdk(items=[], create_state="FAILED")
    monkeypatch.setattr(genai, "_sdk_client", lambda s: sdk)
    s = _settings(project_autocreate=True)
    with pytest.raises(genai.ProjectResolutionError) as ei:
        genai.resolve_project_ocid(s)
    assert "FAILED" in str(ei.value)
    assert genai._project_cache is None


def test_resolution_failure_is_actionable(monkeypatch):
    sdk = FakeSdk(fail=RuntimeError("boom"))
    monkeypatch.setattr(genai, "_sdk_client", lambda s: sdk)
    with pytest.raises(genai.ProjectResolutionError) as ei:
        genai.resolve_project_ocid(_settings())
    msg = str(ei.value)
    assert "PROJECT_OCID" in msg
    assert "generative-ai-project" in msg  # 権限付与の促し


# --- make_inference_client: 空の OpenAi-Project を送らない ---


def test_with_project_never_sends_empty_header(monkeypatch):
    sdk = FakeSdk(fail=RuntimeError("no permission"))
    monkeypatch.setattr(genai, "_sdk_client", lambda s: sdk)
    with pytest.raises(genai.ProjectResolutionError):
        genai.make_inference_client(_settings(), with_project=True)


def test_with_project_uses_resolved_header(monkeypatch):
    sdk = FakeSdk(items=[SimpleNamespace(id="ocid1.generativeaiproject.oc1..live",
                                         lifecycle_state="ACTIVE")])
    monkeypatch.setattr(genai, "_sdk_client", lambda s: sdk)
    c = genai.make_inference_client(_settings(), with_project=True)
    assert c._client.headers["OpenAi-Project"] == "ocid1.generativeaiproject.oc1..live"


def test_explicit_project_arg_wins(monkeypatch):
    def boom(settings):
        raise AssertionError("SDK must not be called with explicit project_ocid")

    monkeypatch.setattr(genai, "_sdk_client", boom)
    c = genai.make_inference_client(
        _settings(), with_project=True, project_ocid="ocid1.generativeaiproject.oc1..agent"
    )
    assert c._client.headers["OpenAi-Project"] == "ocid1.generativeaiproject.oc1..agent"


# --- rag ルートのエラー表面化(500 → ヒント付き 503/502) ---


def _api_error(cls, status):
    req = httpx.Request("POST", "https://genai.test/v1/x")
    resp = httpx.Response(status, request=req, json={"message": "denied"})
    return cls("denied", response=resp, body=None)


def test_upload_surfaces_notfound_as_503(monkeypatch):
    def raise_404(owner, filename, content):
        raise _api_error(openai.NotFoundError, 404)

    monkeypatch.setattr(rag, "add_file", raise_404)
    res = client.post("/api/rag/files", files={"file": ("a.md", b"x", "text/markdown")})
    assert res.status_code == 503
    assert "PROJECT_OCID" in res.json()["detail"]
    assert "404" in res.json()["detail"]


def test_upload_surfaces_badrequest_as_502(monkeypatch):
    def raise_400(owner, filename, content):
        raise _api_error(openai.BadRequestError, 400)

    monkeypatch.setattr(rag, "add_file", raise_400)
    res = client.post("/api/rag/files", files={"file": ("a.md", b"x", "text/markdown")})
    assert res.status_code == 502


def test_upload_surfaces_project_resolution_as_503(monkeypatch):
    def raise_unresolved(owner, filename, content):
        raise genai.ProjectResolutionError("set PROJECT_OCID or grant generative-ai-project")

    monkeypatch.setattr(rag, "add_file", raise_unresolved)
    res = client.post("/api/rag/files", files={"file": ("a.md", b"x", "text/markdown")})
    assert res.status_code == 503
    assert "PROJECT_OCID" in res.json()["detail"]


def test_list_surfaces_permission_denied_as_503(monkeypatch):
    def raise_403(owner):
        raise _api_error(openai.PermissionDeniedError, 403)

    monkeypatch.setattr(rag, "list_files", raise_403)
    res = client.get("/api/rag/files")
    assert res.status_code == 503
    assert "policy" in res.json()["detail"]


# --- /api/rag/health: 3点検査 ---


def _fake_cp(fail=None):
    def _list(**kw):
        if fail:
            raise fail
        return SimpleNamespace(data=[])

    return SimpleNamespace(vector_stores=SimpleNamespace(list=_list))


def _fake_dp(fail=None):
    def _list(**kw):
        if fail:
            raise fail
        return SimpleNamespace(data=[])

    return SimpleNamespace(files=SimpleNamespace(list=_list))


def test_rag_health_all_ok(monkeypatch):
    monkeypatch.setattr(rag, "resolve_project_ocid",
                        lambda **kw: "ocid1.generativeaiproject.oc1..live")
    monkeypatch.setattr(rag, "make_cp_client", lambda: _fake_cp())
    monkeypatch.setattr(rag, "make_inference_client", lambda **kw: _fake_dp())
    res = client.get("/api/rag/health")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["checks"]["project"]["ok"] is True
    assert body["checks"]["control_plane"]["ok"] is True
    assert body["checks"]["data_plane"]["ok"] is True


def test_rag_health_pinpoints_cp_failure(monkeypatch):
    monkeypatch.setattr(rag, "resolve_project_ocid",
                        lambda **kw: "ocid1.generativeaiproject.oc1..live")
    monkeypatch.setattr(rag, "make_cp_client",
                        lambda: _fake_cp(fail=_api_error(openai.NotFoundError, 404)))
    monkeypatch.setattr(rag, "make_inference_client", lambda **kw: _fake_dp())
    body = client.get("/api/rag/health").json()
    assert body["ok"] is False
    assert body["checks"]["control_plane"]["ok"] is False
    assert "404" in body["checks"]["control_plane"]["hint"]
    assert body["checks"]["data_plane"]["ok"] is True  # 失敗点の特定(CPだけ落ちる)


def test_rag_health_project_unresolved_skips_dp(monkeypatch):
    def raise_unresolved(**kw):
        raise genai.ProjectResolutionError("set PROJECT_OCID")

    monkeypatch.setattr(rag, "resolve_project_ocid", raise_unresolved)
    monkeypatch.setattr(rag, "make_cp_client", lambda: _fake_cp())
    body = client.get("/api/rag/health").json()
    assert body["ok"] is False
    assert body["checks"]["project"]["ok"] is False
    assert "PROJECT_OCID" in body["checks"]["project"]["hint"]
    assert body["checks"]["data_plane"]["ok"] is False  # project無しでDPは検査不能
