"""EXB-02: RAG Reference Implementation Descriptor + 静的 Catalog ローダーの検証。

Descriptor 実体は packages/api/jetuse_platform/reference_descriptors/descriptors/(同梱)。
ローダーは jetuse_platform.reference_descriptors。EXB-01 のスキーマ語彙と自己整合すること、
import 時に FS へ触れないこと、公開 API がコピー隔離されることを担保する。
"""

import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jetuse_core.settings import get_settings
from jetuse_platform.contracts import (
    validate_action_with_citations_input,
    validate_action_with_citations_output,
)
from jetuse_platform.reference_descriptors import (
    catalog as catalog_loader,
)
from jetuse_platform.reference_descriptors import (
    get_capability,
    list_capabilities,
    verify_descriptors,
)
from service.main import app

client = TestClient(app)

# packages/api(subprocess で jetuse_platform を import 可能にするための anchor)
_API_DIR = Path(__file__).resolve().parents[1]

_RAG_ID = "rag.answer"
_RAG_VERSION = "1.0.0"


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    # AUTH_REQUIRED 等の monkeypatch が他テストへ漏れないようにする(test_service と同作法)。
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ----------------------------------------------------------------- ローダー
def test_list_capabilities_contains_rag_answer():
    caps = list_capabilities()
    ids = {(c["id"], c["version"]) for c in caps}
    assert (_RAG_ID, _RAG_VERSION) in ids


def test_get_capability_known():
    desc = get_capability(_RAG_ID, _RAG_VERSION)
    assert desc["id"] == _RAG_ID
    assert desc["executionMode"] == "stream"
    assert desc["experienceChannels"] == ["web"]
    assert desc["limitations"] and desc["handoffTriggers"]
    assert "support-answer-with-citations" in desc["supportedScenarios"]


def test_get_capability_unknown_id_raises():
    with pytest.raises(KeyError):
        get_capability("nope.capability", _RAG_VERSION)


def test_get_capability_unknown_version_raises():
    with pytest.raises(KeyError):
        get_capability(_RAG_ID, "9.9.9")


# ------------------------------------------------------ EXB-01 との自己整合
def test_verify_descriptors_schema_refs_exist():
    # 参照する config/input/output/event スキーマ名が EXB-01 に実在すること。
    verify_descriptors()  # 不在なら FileNotFoundError / 不整合なら ValueError


def _tamper(monkeypatch, **overrides):
    """同梱 Descriptor を上書きした 1 件だけを持つ索引に差し替える。"""
    desc = get_capability(_RAG_ID, _RAG_VERSION)
    desc.update(overrides)
    monkeypatch.setattr(
        catalog_loader,
        "_load_descriptors_cached",
        lambda: {(desc["id"], desc["version"]): desc},
    )


def test_verify_descriptors_detects_bad_action(monkeypatch):
    # action を存在しない名に変えると、schema 対応が崩れて検知される。
    _tamper(monkeypatch, action="nonexistent.action@1")
    with pytest.raises(ValueError):
        catalog_loader.verify_descriptors()


def test_verify_descriptors_detects_malformed_action(monkeypatch):
    _tamper(monkeypatch, action="no-version-marker")
    with pytest.raises(ValueError):
        catalog_loader.verify_descriptors()


@pytest.mark.parametrize(
    "action",
    [
        "answer.with-citations@999",  # 未知 version
        "answer.with-citations@",     # version 空
        "answer.with-citations@1@x",  # @ が複数
        "@1",                          # name 空
        "nonexistent.action@1",       # 未知 name
    ],
)
def test_verify_descriptors_detects_bad_action_version(monkeypatch, action):
    _tamper(monkeypatch, action=action)
    with pytest.raises(ValueError):
        catalog_loader.verify_descriptors()


def test_verify_descriptors_detects_unknown_scenario(monkeypatch):
    _tamper(monkeypatch, supportedScenarios=["totally-unknown-scenario"])
    with pytest.raises(ValueError):
        catalog_loader.verify_descriptors()


def test_verify_descriptors_detects_empty_scenarios(monkeypatch):
    _tamper(monkeypatch, supportedScenarios=[])
    with pytest.raises(ValueError):
        catalog_loader.verify_descriptors()


def test_index_descriptors_rejects_duplicate_id_version():
    d = get_capability(_RAG_ID, _RAG_VERSION)
    with pytest.raises(ValueError):
        catalog_loader._index_descriptors([d, dict(d)])


def test_descriptor_example_input_output_validate():
    # Descriptor の例が EXB-01 バリデータを通る(契約の自己整合)。
    example = get_capability(_RAG_ID, _RAG_VERSION)["examples"][0]
    validate_action_with_citations_input(example["input"])
    validate_action_with_citations_output(example["output"])


# ----------------------------------------------------------------- ルート
def test_route_list_capabilities_200():
    res = client.get("/api/v1/catalog/capabilities")
    assert res.status_code == 200
    caps = res.json()["capabilities"]
    assert any(c["id"] == _RAG_ID and c["version"] == _RAG_VERSION for c in caps)


def test_route_get_capability_200():
    res = client.get(f"/api/v1/catalog/capabilities/{_RAG_ID}/versions/{_RAG_VERSION}")
    assert res.status_code == 200
    assert res.json()["id"] == _RAG_ID


def test_route_get_capability_unknown_id_404():
    res = client.get(f"/api/v1/catalog/capabilities/nope/versions/{_RAG_VERSION}")
    assert res.status_code == 404


def test_route_get_capability_unknown_version_404():
    res = client.get(f"/api/v1/catalog/capabilities/{_RAG_ID}/versions/9.9.9")
    assert res.status_code == 404


def test_route_requires_auth_when_enabled(monkeypatch):
    # AUTH_REQUIRED=true の下では無認証アクセスが拒否される(F-001 認証迂回の防止)。
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    get_settings.cache_clear()
    assert client.get("/api/v1/catalog/capabilities").status_code == 401
    assert (
        client.get(f"/api/v1/catalog/capabilities/{_RAG_ID}/versions/{_RAG_VERSION}").status_code
        == 401
    )


# ----------------------------------------------------- import 健全性 / コピー隔離
def test_import_descriptors_is_lazy_no_fs_at_import():
    # import 単体が成功し、初回アクセス前は Descriptor 未読込(遅延)であることを実証。
    code = (
        "import jetuse_platform.reference_descriptors as rd\n"
        "import jetuse_platform.reference_descriptors.catalog as cat\n"
        "assert cat._load_descriptors_cached.cache_info().currsize == 0, 'read at import'\n"
        "assert len(rd.list_capabilities()) >= 1\n"
        "assert cat._load_descriptors_cached.cache_info().currsize == 1\n"
    )
    r = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=_API_DIR
    )
    assert r.returncode == 0, r.stderr


def test_get_capability_returns_isolated_copy():
    first = get_capability(_RAG_ID, _RAG_VERSION)
    first["displayName"] = "HACKED"
    first["limitations"].append("hacked")
    second = get_capability(_RAG_ID, _RAG_VERSION)
    assert second["displayName"] != "HACKED"
    assert "hacked" not in second["limitations"]


def test_wheel_bundles_descriptors(tmp_path):
    # package-data 設定が壊れたら検知: 実 wheel に descriptors/*.json が同梱されることを証明。
    # ビルド失敗は原則 fail(pyproject 破損/パッケージ欠落を検知)。--no-deps ゆえ依存取得もない。
    # skip は pip 自体を起動できない場合(FileNotFoundError)のみに限定する。
    import zipfile

    try:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "-w", str(tmp_path)],
            capture_output=True,
            text=True,
            cwd=_API_DIR,
        )
    except FileNotFoundError as e:  # pip 起動不能のみ skip
        pytest.skip(f"pip 起動不能: {e}")
    assert r.returncode == 0, f"wheel build failed:\n{r.stderr[-3000:]}"

    wheels = list(tmp_path.glob("jetuse*api*.whl"))
    assert wheels, f"wheel が生成されない: {[p.name for p in tmp_path.iterdir()]}"
    with zipfile.ZipFile(wheels[0]) as zf:
        names = set(zf.namelist())
    assert "jetuse_platform/reference_descriptors/descriptors/rag-answer.json" in names, (
        f"descriptor が wheel に未同梱: {sorted(n for n in names if 'descriptors' in n)}"
    )
