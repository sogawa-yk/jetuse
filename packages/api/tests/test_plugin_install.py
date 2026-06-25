"""スナップショット取込 / アンインストールの E2E 単体テスト(PLG-03)。

モックレジストリ(fake transport)＋インメモリ ADB(installed_plugins / usecases / agents)で、
受け入れ条件の中核を実行する:

  install → contributes が ADB(usecases/agents)に出現 → uninstall → 消滅
  署名不正 manifest の取込は拒否され、ADB に何も書かれない

実 ADB へは接続しない(実機 E2E は完了ゲートで別途実施)。ここは「取込ロジックが
署名検証を通したうえで版固定の出所付き定義を書き、uninstall で除去する」契約の検証。
"""

import base64
import contextlib
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from jetuse_core import agents, usecases
from jetuse_core.plugins import installer, store
from jetuse_core.plugins.installer import (
    AlreadyInstalled,
    SignatureRejected,
    install,
    uninstall,
)
from jetuse_core.plugins.manifest import (
    SCHEMA_VERSION,
    canonical_signing_payload,
    validate_manifest,
)
from jetuse_core.plugins.registry_client import INDEX_PATH, RegistryClient

# --- 署名付き manifest を作るヘルパ -------------------------------------------


def _sign(manifest_dict: dict, private_key: Ed25519PrivateKey, key_id: str) -> dict:
    """manifest_dict に発行者署名(ed25519)を付けた dict を返す。"""
    unsigned = validate_manifest(manifest_dict)
    payload = canonical_signing_payload(unsigned)
    sig = private_key.sign(payload)
    signed = dict(manifest_dict)
    signed["signature"] = {
        "algorithm": "ed25519",
        "publicKeyId": key_id,
        "value": base64.b64encode(sig).decode(),
    }
    # 署名込みでも検証を通る(構文整合の確認)。
    validate_manifest(signed)
    return signed


def _usecase_manifest(version="1.2.0"):
    return {
        "schemaVersion": SCHEMA_VERSION,
        "id": "acme/faq",
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
    }


def _agent_manifest(version="1.0.0"):
    return {
        "schemaVersion": SCHEMA_VERSION,
        "id": "acme/helper",
        "version": version,
        "kind": "agent",
        "name": "ヘルパー",
        "publisher": "acme-corp",
        "jetuse": {"minVersion": "0.3.0"},
        "contributes": {
            "agent": {
                "instructions": "親切に手伝う",
                "model": "gpt-oss-120b",
            }
        },
    }


def make_signed_registry(manifest_dicts):
    """署名付き manifest を配るレジストリと、対応する RegistryClient を返す。"""
    private_key = Ed25519PrivateKey.generate()
    pub_raw = private_key.public_key().public_bytes_raw()
    key_id = "acme-key-1"

    files: dict[str, bytes] = {}
    plugins = []
    for md in manifest_dicts:
        signed = _sign(md, private_key, key_id)
        path = f"plugins/{md['id']}/{md['version']}/manifest.json"
        files[path] = json.dumps(signed).encode("utf-8")
        plugins.append(
            {"id": md["id"], "version": md["version"], "kind": md["kind"],
             "name": md["name"], "publisher": md["publisher"], "manifest": path}
        )
    index = {
        "schemaVersion": "1",
        "plugins": plugins,
        "publisherKeys": {key_id: base64.b64encode(pub_raw).decode()},
    }
    files[INDEX_PATH] = json.dumps(index).encode("utf-8")

    client = RegistryClient(base_url="https://reg.example/jetuse/", transport=lambda p: files[p])
    return client, private_key, key_id, files


# --- インメモリ ADB(installed_plugins / usecases / agents) --------------------

_INSTALLED_COLS = [
    "id", "plugin_id", "version", "kind", "source_registry", "manifest",
    "signature_verified", "installed_by", "installed_at",
]


class FakeDB:
    def __init__(self):
        self.installed: list[dict] = []
        self.usecases: list[dict] = []
        self.agents: list[dict] = []
        self._seq = 0


class FakeCursor:
    def __init__(self, db: FakeDB):
        self.db = db
        self.rowcount = 0
        self._result: list[tuple] = []

    def execute(self, sql: str, **b):
        s = " ".join(sql.split())
        if s.startswith("INSERT INTO installed_plugins"):
            if any(r["plugin_id"] == b["pid"] and r["version"] == b["ver"]
                   for r in self.db.installed):
                raise AssertionError("duplicate (plugin_id, version)")
            self.db._seq += 1
            self.db.installed.append({
                "id": b["id"], "plugin_id": b["pid"], "version": b["ver"],
                "kind": b["kind"], "source_registry": b["reg"], "manifest": b["man"],
                "signature_verified": b["sig"], "installed_by": b["installer"],
                "installed_at": self.db._seq,
            })
            self.rowcount = 1
        elif s.startswith("SELECT") and "FROM installed_plugins" in s:
            rows = self.db.installed
            if "WHERE id = :id" in s:
                rows = [r for r in rows if r["id"] == b["id"]]
            elif "plugin_id = :pid AND version = :ver" in s:
                rows = [r for r in rows
                        if r["plugin_id"] == b["pid"] and r["version"] == b["ver"]]
            self._result = [tuple(r[c] for c in _INSTALLED_COLS) for r in rows]
        elif s.startswith("DELETE FROM installed_plugins"):
            before = len(self.db.installed)
            self.db.installed = [r for r in self.db.installed if r["id"] != b["id"]]
            self.rowcount = before - len(self.db.installed)
        elif s.startswith("INSERT INTO usecases"):
            self.db.usecases.append({
                "id": b["id"], "owner_sub": b["o"], "name": b["n"],
                "definition": b["payload"], "visibility": b["v"],
                "source_plugin_id": b["spid"], "source_version": b["sver"],
            })
            self.rowcount = 1
        elif s.startswith("DELETE FROM usecases") and "WHERE id = :id" in s:
            # delete_ingested: id 指定 + 取込行(source_plugin_id IS NOT NULL)限定。
            before = len(self.db.usecases)
            self.db.usecases = [
                r for r in self.db.usecases
                if not (r["id"] == b["id"] and r["source_plugin_id"] is not None)
            ]
            self.rowcount = before - len(self.db.usecases)
        elif s.startswith("DELETE FROM usecases"):
            before = len(self.db.usecases)
            self.db.usecases = [
                r for r in self.db.usecases
                if not (r["source_plugin_id"] == b["spid"]
                        and r["source_version"] == b["sver"])
            ]
            self.rowcount = before - len(self.db.usecases)
        elif s.startswith("INSERT INTO agents"):
            self.db.agents.append({
                "id": b["id"], "owner_sub": b["o"], "name": b["n"],
                "instructions": b["ins"], "model": b["m"], "visibility": b["v"],
                "source_plugin_id": b["spid"], "source_version": b["sver"],
            })
            self.rowcount = 1
        elif s.startswith("DELETE FROM agents") and "WHERE id = :id" in s:
            before = len(self.db.agents)
            self.db.agents = [
                r for r in self.db.agents
                if not (r["id"] == b["id"] and r["source_plugin_id"] is not None)
            ]
            self.rowcount = before - len(self.db.agents)
        elif s.startswith("DELETE FROM agents"):
            before = len(self.db.agents)
            self.db.agents = [
                r for r in self.db.agents
                if not (r["source_plugin_id"] == b["spid"]
                        and r["source_version"] == b["sver"])
            ]
            self.rowcount = before - len(self.db.agents)
        else:  # pragma: no cover
            raise AssertionError(f"unexpected SQL: {s}")

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)


class FakeConn:
    def __init__(self, db: FakeDB):
        self.db = db

    def cursor(self):
        return FakeCursor(self.db)

    def commit(self):
        pass


@pytest.fixture
def fake_db(monkeypatch):
    db = FakeDB()

    @contextlib.contextmanager
    def fake_connect():
        yield FakeConn(db)

    # store / usecases / agents が共有する 1 つのインメモリ ADB に向ける。
    monkeypatch.setattr(store, "connect", fake_connect)
    monkeypatch.setattr(usecases, "connect", fake_connect)
    monkeypatch.setattr(agents, "connect", fake_connect)
    return db


# --- E2E: install → ADB 出現 → uninstall → 消滅 ------------------------------


def test_install_usecase_then_uninstall_roundtrip(fake_db):
    client, *_ = make_signed_registry([_usecase_manifest()])

    rec = install(client, "acme/faq", "1.2.0", installed_by="sa@example.com")

    # installed_plugins に署名検証済みで記録される。
    assert rec["plugin_id"] == "acme/faq"
    assert rec["version"] == "1.2.0"
    assert rec["signature_verified"] is True
    assert rec["source_registry"] == "https://reg.example/jetuse/"
    assert rec["ingested"] == [("usecases", fake_db.usecases[0]["id"])]

    # contributes が usecases(ADB)に版固定・出所付きで出現する。
    assert len(fake_db.usecases) == 1
    uc = fake_db.usecases[0]
    assert uc["source_plugin_id"] == "acme/faq"
    assert uc["source_version"] == "1.2.0"
    assert uc["owner_sub"] == "sa@example.com"  # owner 未指定なら installed_by
    definition = json.loads(uc["definition"])
    assert definition["template"] == "要約して: {{text}}"
    assert definition["name"] == "FAQ要約"  # manifest トップレベルから補完

    # uninstall で取込定義もインストール記録も消える。
    assert uninstall("acme/faq", "1.2.0") is True
    assert fake_db.usecases == []
    assert fake_db.installed == []

    # 二重 uninstall は False(記録が無い)。取込定義削除は冪等。
    assert uninstall("acme/faq", "1.2.0") is False


def test_install_agent_then_uninstall_roundtrip(fake_db):
    client, *_ = make_signed_registry([_agent_manifest()])

    rec = install(client, "acme/helper", "1.0.0", installed_by="sa", owner="team")
    assert rec["kind"] == "agent"
    assert len(fake_db.agents) == 1
    ag = fake_db.agents[0]
    assert ag["source_plugin_id"] == "acme/helper"
    assert ag["source_version"] == "1.0.0"
    assert ag["owner_sub"] == "team"
    assert ag["instructions"] == "親切に手伝う"

    assert uninstall("acme/helper", "1.0.0") is True
    assert fake_db.agents == []
    assert fake_db.installed == []


def test_install_resolves_latest_version_when_unspecified(fake_db):
    client, *_ = make_signed_registry(
        [_usecase_manifest(version="1.0.0"), _usecase_manifest(version="2.0.0")]
    )
    rec = install(client, "acme/faq", installed_by="sa")
    assert rec["version"] == "2.0.0"
    assert fake_db.usecases[0]["source_version"] == "2.0.0"


# --- 署名不正は取込拒否(ADB に何も書かれない) -------------------------------


def test_tampered_signature_is_rejected(fake_db):
    client, *_ = make_signed_registry([_usecase_manifest()])
    # 元の署名は残したまま配布 manifest の本文だけ書き換える → 署名不一致(改ざん)。
    path = "plugins/acme/faq/1.2.0/manifest.json"
    served = json.loads(client._fetch(path))
    served["contributes"]["usecase"]["template"] = "改ざん: {{text}}"

    def transport(p):
        if p == path:
            return json.dumps(served).encode()
        return client._fetch(p)

    tampered_client = RegistryClient(base_url="https://reg/", transport=transport)
    with pytest.raises(SignatureRejected):
        install(tampered_client, "acme/faq", "1.2.0", installed_by="sa")
    # ADB には一切書かれない(fail-closed)。
    assert fake_db.usecases == []
    assert fake_db.installed == []


def test_unsigned_manifest_is_rejected(fake_db):
    # 署名フィールドを持たない manifest を配る → 取込拒否。
    md = _usecase_manifest()
    path = f"plugins/{md['id']}/{md['version']}/manifest.json"
    index = {
        "schemaVersion": "1",
        "plugins": [{"id": md["id"], "version": md["version"], "kind": md["kind"],
                     "name": md["name"], "publisher": md["publisher"], "manifest": path}],
        "publisherKeys": {},
    }
    files = {path: json.dumps(md).encode(), INDEX_PATH: json.dumps(index).encode()}
    client = RegistryClient(base_url="https://reg/", transport=lambda p: files[p])
    with pytest.raises(SignatureRejected):
        install(client, "acme/faq", "1.2.0", installed_by="sa")
    assert fake_db.usecases == []
    assert fake_db.installed == []


def test_wrong_public_key_is_rejected(fake_db):
    # 正しく署名するが、index の公開鍵を別物に差し替える → 検証失敗で拒否。
    client, _priv, key_id, files = make_signed_registry([_usecase_manifest()])
    other_pub = Ed25519PrivateKey.generate().public_key().public_bytes_raw()
    index = json.loads(files[INDEX_PATH])
    index["publisherKeys"][key_id] = base64.b64encode(other_pub).decode()
    files[INDEX_PATH] = json.dumps(index).encode()
    bad_client = RegistryClient(base_url="https://reg/", transport=lambda p: files[p])
    with pytest.raises(SignatureRejected):
        install(bad_client, "acme/faq", "1.2.0", installed_by="sa")
    assert fake_db.usecases == []
    assert fake_db.installed == []


def test_unknown_public_key_id_is_rejected_as_signature(fake_db):
    # 署名は付くが publicKeyId が registry 未登録 → RegistryError ではなく SignatureRejected に
    # 正規化される(Codex F-002)。
    client, _priv, key_id, files = make_signed_registry([_usecase_manifest()])
    index = json.loads(files[INDEX_PATH])
    index["publisherKeys"] = {}  # 公開鍵を空にして lookup を失敗させる。
    files[INDEX_PATH] = json.dumps(index).encode()
    bad_client = RegistryClient(base_url="https://reg/", transport=lambda p: files[p])
    with pytest.raises(SignatureRejected):
        install(bad_client, "acme/faq", "1.2.0", installed_by="sa")
    assert fake_db.usecases == []
    assert fake_db.installed == []


def test_duplicate_install_is_rejected_and_keeps_existing(fake_db):
    # 同一 (plugin_id, version) の二重 install は AlreadyInstalled で拒否し、既存取込を保つ。
    client, *_ = make_signed_registry([_usecase_manifest()])
    install(client, "acme/faq", "1.2.0", installed_by="sa")
    assert len(fake_db.usecases) == 1 and len(fake_db.installed) == 1
    with pytest.raises(AlreadyInstalled):
        install(client, "acme/faq", "1.2.0", installed_by="sa")
    # 取込前に弾くので既存の取込定義もインストール記録も増減しない。
    assert len(fake_db.usecases) == 1
    assert len(fake_db.installed) == 1


def test_record_failure_compensation_only_removes_new_defs(fake_db, monkeypatch):
    # record_install が失敗しても、補償削除は「いま作った行」だけに限定され、同一 plugin_id の
    # 別版の既存取込定義を巻き込まない(Codex F-001 / blocker の回帰防止)。
    client, *_ = make_signed_registry(
        [_usecase_manifest(version="1.0.0"), _usecase_manifest(version="2.0.0")]
    )
    install(client, "acme/faq", "1.0.0", installed_by="sa")  # 既存(別版)
    assert len(fake_db.usecases) == 1
    existing_id = fake_db.usecases[0]["id"]

    def boom(*a, **k):
        raise RuntimeError("simulated record_install failure")

    monkeypatch.setattr(installer.store, "record_install", boom)
    with pytest.raises(RuntimeError):
        install(client, "acme/faq", "2.0.0", installed_by="sa")
    # 2.0.0 の取込定義だけが補償削除され、1.0.0 の既存定義は残る。
    assert [r["id"] for r in fake_db.usecases] == [existing_id]
    assert fake_db.usecases[0]["source_version"] == "1.0.0"
