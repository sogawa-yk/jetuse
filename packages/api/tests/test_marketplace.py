"""マーケットプレイス API(PLG-06)のルートテスト。

レジストリ通信(RegistryClient)・取込(installer)・記録(store)はフェイクに差し替え、
カタログ合成(インストール状態・更新有無)と HTTP エラー正規化、install/uninstall 経路を検証する。
実 DB・実レジストリへは接続しない(実機 E2E は完了ゲートで別途)。
"""

import pytest
from fastapi.testclient import TestClient

import service.routes.marketplace as mp
from jetuse_core.plugins.installer import AlreadyInstalled, SignatureRejected
from jetuse_core.plugins.manifest import SCHEMA_VERSION, validate_manifest
from service.main import app

client = TestClient(app)


def _entry(pid, version, kind="usecase", name="FAQ", tags=None):
    return {
        "id": pid,
        "version": version,
        "kind": kind,
        "name": name,
        "description": f"{name} desc",
        "publisher": "acme-corp",
        "tags": tags or ["faq"],
        "manifest": f"plugins/{pid}/{version}/manifest.json",
    }


def _manifest(pid="acme/faq", version="1.2.0"):
    return validate_manifest(
        {
            "schemaVersion": SCHEMA_VERSION,
            "id": pid,
            "version": version,
            "kind": "usecase",
            "name": "FAQ要約",
            "description": "FAQを要約する",
            "publisher": "acme-corp",
            "jetuse": {"minVersion": "0.3.0"},
            "permissions": ["platform:rag.search"],
            "contributes": {
                "usecase": {
                    "fields": [{"name": "text", "type": "textarea"}],
                    "template": "要約して: {{text}}",
                }
            },
            "tags": ["faq"],
        }
    )


class FakeClient:
    def __init__(self, entries, manifest=None):
        self.entries = entries
        self.manifest = manifest or _manifest()

    def list(self):
        return [dict(e) for e in self.entries]

    def download(self, plugin_id, version=None):
        return self.manifest


@pytest.fixture
def wire(monkeypatch):
    """build_client / store / installer をフェイクに差し替える共通フィクスチャ。"""
    state = {
        "client": FakeClient([_entry("acme/faq", "1.0.0"), _entry("acme/faq", "1.2.0")]),
        "installs": [],
        "install_calls": [],
        "uninstall_calls": [],
        "install_raises": None,
    }
    monkeypatch.setattr(mp, "build_client", lambda settings: state["client"])
    monkeypatch.setattr(mp.store, "list_installs", lambda pid=None: [
        r for r in state["installs"] if pid is None or r["plugin_id"] == pid
    ])

    def fake_find_install(plugin_id, version):
        for r in state["installs"]:
            if r["plugin_id"] == plugin_id and r["version"] == version:
                return r
        return None

    monkeypatch.setattr(mp.store, "find_install", fake_find_install)

    def fake_install(client, plugin_id, version=None, *, installed_by, owner=None,
                     authorize=None, **_):
        state["install_calls"].append((plugin_id, version, installed_by))
        # 実 installer と同様、署名検証済み manifest に認可フックを呼ぶ（TOCTOU 回避の検証）。
        if authorize is not None:
            authorize(client.download(plugin_id, version))
        if state["install_raises"]:
            raise state["install_raises"]
        rec = {
            "plugin_id": plugin_id,
            "version": version or "1.2.0",
            "kind": "usecase",
            "ingested": [("usecases", "uc-1")],
        }
        state["installs"].append(
            {"plugin_id": plugin_id, "version": rec["version"], "installed_by": installed_by}
        )
        return rec

    def fake_uninstall(plugin_id, version):
        state["uninstall_calls"].append((plugin_id, version))
        before = len(state["installs"])
        state["installs"] = [
            r for r in state["installs"]
            if not (r["plugin_id"] == plugin_id and r["version"] == version)
        ]
        return len(state["installs"]) < before

    monkeypatch.setattr(mp.installer, "install", fake_install)
    monkeypatch.setattr(mp.installer, "uninstall", fake_uninstall)
    return state


# --- 純粋関数 --------------------------------------------------------------


def test_build_catalog_picks_latest_and_marks_update():
    available = [_entry("acme/faq", "1.0.0"), _entry("acme/faq", "1.2.0")]
    installs = [{"plugin_id": "acme/faq", "version": "1.0.0"}]
    cards = mp.build_catalog(available, installs)
    assert len(cards) == 1
    card = cards[0]
    assert card["version"] == "1.2.0"  # 最新が代表
    assert card["installed"] is True
    assert card["installed_versions"] == ["1.0.0"]
    assert card["update_available"] is True  # 1.0.0 導入済 < 最新 1.2.0
    assert card["versions"] == ["1.2.0", "1.0.0"]


def test_build_catalog_no_update_when_latest_installed():
    available = [_entry("acme/faq", "1.2.0")]
    installs = [{"plugin_id": "acme/faq", "version": "1.2.0"}]
    card = mp.build_catalog(available, installs)[0]
    assert card["installed"] is True
    assert card["update_available"] is False


def test_build_catalog_no_update_on_downgrade_or_missing_version():
    # 導入済み 2.0.0 だが現行 index の最新が 1.2.0(降格 / 旧版が index から消えた)→ 更新なし。
    available = [_entry("acme/faq", "1.2.0")]
    installs = [{"plugin_id": "acme/faq", "version": "2.0.0"}]
    card = mp.build_catalog(available, installs)[0]
    assert card["installed"] is True
    assert card["update_available"] is False  # semver 比較で latest <= 導入済み最大


def test_build_catalog_installable_by_kind():
    # MKT-01: sample-app / connector もマーケットからインストール可能(installer が取込先へ展開)。
    cards = mp.build_catalog(
        [_entry("acme/uc", "1.0.0", kind="usecase"),
         _entry("acme/ag", "1.0.0", kind="agent"),
         _entry("acme/sa", "1.0.0", kind="sample-app"),
         _entry("acme/conn", "1.0.0", kind="connector"),
         _entry("acme/unknown", "1.0.0", kind="hosted-app")],
        [],
    )
    by_id = {c["id"]: c for c in cards}
    assert by_id["acme/uc"]["installable"] is True
    assert by_id["acme/ag"]["installable"] is True
    assert by_id["acme/sa"]["installable"] is True
    assert by_id["acme/conn"]["installable"] is True
    # 未対応 kind は installable=False のまま(UI は install を無効化)。
    assert by_id["acme/unknown"]["installable"] is False


def test_build_catalog_can_uninstall_only_for_owner():
    available = [_entry("acme/faq", "1.0.0")]
    installs = [{"plugin_id": "acme/faq", "version": "1.0.0", "installed_by": "alice"}]
    bob = mp.build_catalog(available, installs, viewer="bob")[0]
    alice = mp.build_catalog(available, installs, viewer="alice")[0]
    assert bob["installed"] is True and bob["can_uninstall"] is False
    assert alice["can_uninstall"] is True


def test_filter_catalog_by_q_tag_kind():
    cards = mp.build_catalog(
        [_entry("acme/faq", "1.0.0", name="FAQ", tags=["faq"]),
         _entry("acme/sum", "1.0.0", kind="agent", name="Summarizer", tags=["text"])],
        [],
    )
    assert {c["id"] for c in mp.filter_catalog(cards, q="summ")} == {"acme/sum"}
    assert {c["id"] for c in mp.filter_catalog(cards, tag="faq")} == {"acme/faq"}
    assert {c["id"] for c in mp.filter_catalog(cards, kind="agent")} == {"acme/sum"}


# --- ルート ----------------------------------------------------------------


def test_list_marketplace(wire):
    res = client.get("/api/marketplace/plugins")
    assert res.status_code == 200
    body = res.json()
    assert body["plugins"][0]["id"] == "acme/faq"
    assert body["plugins"][0]["version"] == "1.2.0"
    assert "faq" in body["tags"]


def test_list_marketplace_search(wire):
    assert client.get("/api/marketplace/plugins?q=nope").json()["plugins"] == []
    assert len(client.get("/api/marketplace/plugins?q=faq").json()["plugins"]) == 1


def test_detail_includes_permissions(wire):
    res = client.get("/api/marketplace/plugins/acme/faq")
    assert res.status_code == 200
    body = res.json()
    assert body["permissions"] == ["platform:rag.search"]
    assert body["signed"] is False
    assert body["versions"] == ["1.2.0", "1.0.0"]


def test_detail_404_when_unknown(wire):
    wire["client"] = FakeClient([])
    assert client.get("/api/marketplace/plugins/no/such").status_code == 404


def test_install_then_uninstall_flow(wire):
    # install
    res = client.post("/api/marketplace/install", json={"plugin_id": "acme/faq"})
    assert res.status_code == 200
    assert res.json()["installed"] is True
    assert wire["install_calls"] == [("acme/faq", None, "dev-user")]
    # 一覧に installed が反映される
    card = client.get("/api/marketplace/plugins").json()["plugins"][0]
    assert card["installed"] is True
    # uninstall
    res = client.post(
        "/api/marketplace/uninstall",
        json={"plugin_id": "acme/faq", "version": "1.2.0"},
    )
    assert res.status_code == 200
    assert res.json()["uninstalled"] is True
    assert client.get("/api/marketplace/plugins").json()["plugins"][0]["installed"] is False


def test_install_conflict_returns_409(wire):
    wire["install_raises"] = AlreadyInstalled("既にインストール済み")
    res = client.post("/api/marketplace/install", json={"plugin_id": "acme/faq"})
    assert res.status_code == 409


def test_install_unsigned_returns_422(wire):
    wire["install_raises"] = SignatureRejected("未署名")
    res = client.post("/api/marketplace/install", json={"plugin_id": "acme/faq"})
    assert res.status_code == 422


def test_uninstall_missing_returns_404(wire):
    res = client.post(
        "/api/marketplace/uninstall",
        json={"plugin_id": "acme/faq", "version": "9.9.9"},
    )
    assert res.status_code == 404


def test_uninstall_rejected_for_non_owner(wire):
    # 別ユーザー(someone-else)が入れた install を dev-user は消せない(404・取込定義を守る)。
    wire["installs"].append(
        {"plugin_id": "acme/faq", "version": "1.2.0", "installed_by": "someone-else"}
    )
    res = client.post(
        "/api/marketplace/uninstall",
        json={"plugin_id": "acme/faq", "version": "1.2.0"},
    )
    assert res.status_code == 404
    assert wire["uninstall_calls"] == []  # installer.uninstall は呼ばれない
    # 一覧では installed だが can_uninstall=False(他人の install)。
    card = client.get("/api/marketplace/plugins").json()["plugins"][0]
    assert card["installed"] is True and card["can_uninstall"] is False


def test_registry_unconfigured_returns_503():
    # build_client の実体(未設定→503)を決定的に確認するため、Settings を依存上書きで空 URL に固定。
    from jetuse_core.settings import Settings, get_settings

    app.dependency_overrides[get_settings] = lambda: Settings(plugin_registry_url="")
    try:
        res = client.get("/api/marketplace/plugins")
        assert res.status_code == 503
    finally:
        app.dependency_overrides.pop(get_settings, None)


# --- external-app kind の install 対応（BE-06） ----------------------------


def test_build_catalog_marks_external_app_installable():
    """external-app は SUPPORTED_KINDS に入り installable=True（BE-06）。"""
    available = [_entry("jetuse/denpyon", "1.0.0", kind="external-app", name="伝ぴょん")]
    card = mp.build_catalog(available, [])[0]
    assert card["kind"] == "external-app"
    assert card["installable"] is True


def _external_app_manifest():
    """external-app kind の検証済み manifest（admin ゲートの kind 判定用に download が返す）。"""
    from jetuse_core.plugins.denpyon_external_app import denpyon_external_app_manifest

    return denpyon_external_app_manifest(
        url="https://denpyon.example.com/app",
        issuer="https://idp.example.com",
        audience="https://denpyon.example.com",
    )


def _wire_external_app(wire, monkeypatch):
    """download が external-app manifest を返し、installer.install をフェイクにする。"""
    wire["client"] = FakeClient(
        [_entry("jetuse/denpyon", "1.0.0", kind="external-app", name="伝ぴょん")],
        manifest=_external_app_manifest(),
    )

    def fake_install(client, plugin_id, version=None, *, installed_by, owner=None,
                     authorize=None, **_):
        # 認可フックを署名検証済み manifest に対して**取込前**に呼ぶ（admin ゲート発火点）。
        if authorize is not None:
            authorize(client.download(plugin_id, version))
        wire["install_calls"].append((plugin_id, version, installed_by))
        return {
            "plugin_id": plugin_id,
            "version": version or "1.0.0",
            "kind": "external-app",
            "ingested": [("external_app_instances", "ext-1")],
        }

    monkeypatch.setattr(mp.installer, "install", fake_install)


def test_install_external_app_forbidden_without_admin(wire, monkeypatch):
    """external-app の install は運用者(ADMIN_USERS)ゲート＝非運用者は 403（BE06-AUTHZ-001）。"""
    _wire_external_app(wire, monkeypatch)
    # ADMIN_USERS 空（既定）→ is_admin(dev-user)=False → 403。installer.install は呼ばれない。
    resp = client.post("/api/marketplace/install", json={"plugin_id": "jetuse/denpyon"})
    assert resp.status_code == 403
    assert wire["install_calls"] == []


def test_install_external_app_allowed_for_admin(wire, monkeypatch):
    """運用者（ADMIN_USERS に dev-user）なら external-app を install できる（BE06-AUTHZ-001）。"""
    import service.deps as deps
    from jetuse_core.settings import Settings

    _wire_external_app(wire, monkeypatch)
    # is_admin は get_settings() を直接呼ぶ（Depends 注入ではない）ため deps 側を差し替える。
    monkeypatch.setattr(deps, "get_settings", lambda: Settings(admin_users="dev-user"))
    resp = client.post("/api/marketplace/install", json={"plugin_id": "jetuse/denpyon"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "external-app"
    assert body["ingested"] == [["external_app_instances", "ext-1"]]
    assert wire["install_calls"] == [("jetuse/denpyon", None, "dev-user")]
