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
from jetuse_core.plugins import (
    connector_store,
    external_app_store,
    installer,
    scaffold,
    store,
)
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


def _sample_app_manifest(version="1.0.0"):
    return {
        "schemaVersion": SCHEMA_VERSION,
        "id": "acme/crm-lite",
        "version": version,
        "kind": "sample-app",
        "name": "CRMライト",
        "description": "サンプル CRM",
        "publisher": "acme-corp",
        "jetuse": {"minVersion": "0.3.0"},
        "permissions": ["platform:rag.search"],
        "contributes": {
            "sample-app": {
                "screens": [
                    {"key": "leads", "title": "リード一覧", "type": "list",
                     "dataset": "leads", "slots": ["summarize-lead"]},
                ],
                "datasets": [
                    {"name": "leads", "label": "リード", "fields": [
                        {"name": "company", "type": "string", "required": True},
                        {"name": "amount", "type": "number"},
                    ], "seed": [
                        {"company": "ACME", "amount": 100},
                        {"company": "Globex", "amount": 200},
                    ]},
                ],
                "aiSlots": [
                    {"key": "summarize-lead", "title": "要約", "capability": "summarize",
                     "permissions": ["platform:rag.search"]},
                ],
            }
        },
    }


def _connector_manifest(version="1.0.0", *, permissions=None, action_perms=None):
    return {
        "schemaVersion": SCHEMA_VERSION,
        "id": "acme/slackish",
        "version": version,
        "kind": "connector",
        "name": "Slackish",
        "description": "サンプルコネクタ",
        "publisher": "acme-corp",
        "jetuse": {"minVersion": "0.3.0"},
        "permissions": ["platform:files.read"] if permissions is None else permissions,
        "contributes": {
            "connector": {
                "provider": "slackish",
                "transport": "builtin",
                # 実シークレットは持たず参照名のみ(CON-01 の契約)。
                "auth": {"kind": "api_token", "secretRef": "slackish-token"},
                "actions": [
                    {"name": "post_message", "title": "投稿",
                     "permissions": ["platform:files.read"]
                     if action_perms is None else action_perms},
                ],
            }
        },
    }


def _external_app_manifest(version="1.0.0"):
    return {
        "schemaVersion": SCHEMA_VERSION,
        "id": "acme/denpyon",
        "version": version,
        "kind": "external-app",
        "name": "伝ぴょん 連携",
        "description": "サンプル external-app",
        "publisher": "acme-corp",
        "jetuse": {"minVersion": "0.3.0"},
        "permissions": [],
        "contributes": {
            "external-app": {
                "app": "denpyon",
                "embed": "iframe",
                "url": "https://denpyon.example.com/app",
                "title": "伝ぴょん",
                "sso": {
                    "mode": "oidc",
                    "issuer": "https://idp.example.com",
                    "clientIdRef": "denpyon-oidc-client-id",
                    "secretRef": "denpyon-oidc-client-secret",
                    "audience": "https://denpyon.example.com",
                    "scopes": ["openid", "email"],
                    "claimMapping": {"sub": "preferred_username"},
                },
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


_SAMPLE_APP_COLS = [
    "id", "plugin_id", "source_version", "name", "definition", "created_by", "created_at",
]
_CONNECTOR_COLS = [
    "id", "plugin_id", "source_version", "name", "provider", "transport",
    "definition", "registered_by", "created_at",
]

_EXTERNAL_APP_COLS = [
    "id", "plugin_id", "source_version", "name", "app", "embed",
    "definition", "registered_by", "created_at",
]


class FakeDB:
    def __init__(self):
        self.installed: list[dict] = []
        self.usecases: list[dict] = []
        self.agents: list[dict] = []
        # MKT-01: L2 kind の取込先(scaffold / connector_store)を同じインメモリ ADB で表す。
        self.sample_app_instances: list[dict] = []
        self.sample_app_seed_rows: list[dict] = []
        self.connector_instances: list[dict] = []
        # BE-06: external-app(external_app_store)取込先を同じインメモリ ADB で表す。
        self.external_app_instances: list[dict] = []
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
        # --- MKT-01: sample-app(scaffold)取込先 ---
        elif s.startswith("INSERT INTO sample_app_instances"):
            self.db._seq += 1
            self.db.sample_app_instances.append({
                "id": b["id"], "plugin_id": b["pid"], "source_version": b["ver"],
                "name": b["name"], "definition": b["defn"], "created_by": b["creator"],
                "created_at": self.db._seq,
            })
            self.rowcount = 1
        elif s.startswith("SELECT") and "FROM sample_app_instances" in s:
            rows = self.db.sample_app_instances
            if "WHERE id = :id" in s:
                rows = [r for r in rows if r["id"] == b["id"]]
            self._result = [tuple(r[c] for c in _SAMPLE_APP_COLS) for r in rows]
        elif s.startswith("DELETE FROM sample_app_seed_rows"):
            if "instance_id = :iid" in s:
                keep = lambda r: r["instance_id"] != b["iid"]  # noqa: E731
            else:  # IN (SELECT ... WHERE plugin_id = :pid AND source_version = :ver)
                victim = {
                    r["id"] for r in self.db.sample_app_instances
                    if r["plugin_id"] == b["pid"] and r["source_version"] == b["ver"]
                }
                keep = lambda r: r["instance_id"] not in victim  # noqa: E731
            before = len(self.db.sample_app_seed_rows)
            self.db.sample_app_seed_rows = [
                r for r in self.db.sample_app_seed_rows if keep(r)
            ]
            self.rowcount = before - len(self.db.sample_app_seed_rows)
        elif s.startswith("DELETE FROM sample_app_instances"):
            before = len(self.db.sample_app_instances)
            if "WHERE id = :id" in s:
                self.db.sample_app_instances = [
                    r for r in self.db.sample_app_instances if r["id"] != b["id"]
                ]
            else:  # WHERE plugin_id = :pid AND source_version = :ver
                self.db.sample_app_instances = [
                    r for r in self.db.sample_app_instances
                    if not (r["plugin_id"] == b["pid"] and r["source_version"] == b["ver"])
                ]
            self.rowcount = before - len(self.db.sample_app_instances)
        # --- MKT-01: connector(connector_store)取込先 ---
        elif s.startswith("INSERT INTO connector_instances"):
            self.db._seq += 1
            self.db.connector_instances.append({
                "id": b["id"], "plugin_id": b["pid"], "source_version": b["ver"],
                "name": b["name"], "provider": b["prov"], "transport": b["trans"],
                "definition": b["defn"], "registered_by": b["registrar"],
                "created_at": self.db._seq,
            })
            self.rowcount = 1
        elif s.startswith("SELECT") and "FROM connector_instances" in s:
            rows = self.db.connector_instances
            if "WHERE id = :id" in s:
                rows = [r for r in rows if r["id"] == b["id"]]
            self._result = [tuple(r[c] for c in _CONNECTOR_COLS) for r in rows]
        elif s.startswith("DELETE FROM connector_instances"):
            before = len(self.db.connector_instances)
            if "WHERE id = :id" in s:
                self.db.connector_instances = [
                    r for r in self.db.connector_instances if r["id"] != b["id"]
                ]
            else:  # WHERE plugin_id = :pid AND source_version = :ver
                self.db.connector_instances = [
                    r for r in self.db.connector_instances
                    if not (r["plugin_id"] == b["pid"] and r["source_version"] == b["ver"])
                ]
            self.rowcount = before - len(self.db.connector_instances)
        # --- BE-06: external-app(external_app_store)取込先 ---
        elif s.startswith("INSERT INTO external_app_instances"):
            self.db._seq += 1
            self.db.external_app_instances.append({
                "id": b["id"], "plugin_id": b["pid"], "source_version": b["ver"],
                "name": b["name"], "app": b["app"], "embed": b["embed"],
                "definition": b["defn"], "registered_by": b["registrar"],
                "created_at": self.db._seq,
            })
            self.rowcount = 1
        elif s.startswith("SELECT") and "FROM external_app_instances" in s:
            rows = self.db.external_app_instances
            if "WHERE id = :id" in s:
                rows = [r for r in rows if r["id"] == b["id"]]
            self._result = [tuple(r[c] for c in _EXTERNAL_APP_COLS) for r in rows]
        elif s.startswith("DELETE FROM external_app_instances"):
            before = len(self.db.external_app_instances)
            if "WHERE id = :id" in s:
                self.db.external_app_instances = [
                    r for r in self.db.external_app_instances if r["id"] != b["id"]
                ]
            else:  # WHERE plugin_id = :pid AND source_version = :ver
                self.db.external_app_instances = [
                    r for r in self.db.external_app_instances
                    if not (r["plugin_id"] == b["pid"] and r["source_version"] == b["ver"])
                ]
            self.rowcount = before - len(self.db.external_app_instances)
        else:  # pragma: no cover
            raise AssertionError(f"unexpected SQL: {s}")

    def executemany(self, sql: str, rows):
        s = " ".join(sql.split())
        if s.startswith("INSERT INTO sample_app_seed_rows"):
            # 位置パラメータ (:1..:5) = (id, instance_id, dataset, row_index, payload)
            for r in rows:
                self.db.sample_app_seed_rows.append({
                    "id": r[0], "instance_id": r[1], "dataset": r[2],
                    "row_index": r[3], "payload": r[4],
                })
            self.rowcount = len(rows)
        else:  # pragma: no cover
            raise AssertionError(f"unexpected executemany SQL: {s}")

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

    # store / usecases / agents / scaffold / connector_store が共有する 1 つのインメモリ ADB。
    monkeypatch.setattr(store, "connect", fake_connect)
    monkeypatch.setattr(usecases, "connect", fake_connect)
    monkeypatch.setattr(agents, "connect", fake_connect)
    monkeypatch.setattr(scaffold, "connect", fake_connect)
    monkeypatch.setattr(connector_store, "connect", fake_connect)
    monkeypatch.setattr(external_app_store, "connect", fake_connect)
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


# --- MKT-01: sample-app / connector kind の取込 → 出現 → uninstall → 消滅 -------


def test_install_sample_app_then_uninstall_roundtrip(fake_db):
    client, *_ = make_signed_registry([_sample_app_manifest()])

    rec = install(client, "acme/crm-lite", "1.0.0", installed_by="sa@example.com")

    # 署名検証済みで installed_plugins に記録される(kind=sample-app)。
    assert rec["signature_verified"] is True
    assert rec["version"] == "1.0.0"
    inst_id = fake_db.sample_app_instances[0]["id"]
    assert rec["ingested"] == [("sample_app_instances", inst_id)]

    # scaffold が版固定・出所付きでインスタンスを展開し、seed 行も展開される。
    inst = fake_db.sample_app_instances[0]
    assert inst["plugin_id"] == "acme/crm-lite"
    assert inst["source_version"] == "1.0.0"
    assert inst["created_by"] == "sa@example.com"
    assert len(fake_db.sample_app_seed_rows) == 2  # leads dataset の seed 2 行
    definition = json.loads(inst["definition"])
    assert definition["screens"][0]["key"] == "leads"
    assert definition["aiSlots"][0]["capability"] == "summarize"

    # uninstall でインスタンス・seed 行・インストール記録がすべて消える。
    assert uninstall("acme/crm-lite", "1.0.0") is True
    assert fake_db.sample_app_instances == []
    assert fake_db.sample_app_seed_rows == []
    assert store.find_install("acme/crm-lite", "1.0.0") is None


def test_install_connector_then_uninstall_roundtrip(fake_db):
    client, *_ = make_signed_registry([_connector_manifest()])

    rec = install(client, "acme/slackish", "1.0.0", installed_by="sa@example.com")

    assert rec["signature_verified"] is True
    inst_id = fake_db.connector_instances[0]["id"]
    assert rec["ingested"] == [("connector_instances", inst_id)]

    inst = fake_db.connector_instances[0]
    assert inst["plugin_id"] == "acme/slackish"
    assert inst["source_version"] == "1.0.0"
    assert inst["provider"] == "slackish"
    assert inst["registered_by"] == "sa@example.com"
    definition = json.loads(inst["definition"])
    assert definition["actions"][0]["name"] == "post_message"
    # 認証は参照名のみ(実シークレット値は保存しない = CON-01 の契約)。
    assert definition["auth"]["secretRef"] == "slackish-token"
    assert "kind" in definition["auth"]

    assert uninstall("acme/slackish", "1.0.0") is True
    assert fake_db.connector_instances == []
    assert store.find_install("acme/slackish", "1.0.0") is None


def test_install_external_app_then_uninstall_roundtrip(fake_db):
    """BE-06: 署名付き external-app を install → external_app_instances 出現 → uninstall で消滅。"""
    client, *_ = make_signed_registry([_external_app_manifest()])

    rec = install(client, "acme/denpyon", "1.0.0", installed_by="sa@example.com")

    assert rec["signature_verified"] is True
    inst_id = fake_db.external_app_instances[0]["id"]
    assert rec["ingested"] == [("external_app_instances", inst_id)]

    inst = fake_db.external_app_instances[0]
    assert inst["plugin_id"] == "acme/denpyon"
    assert inst["source_version"] == "1.0.0"
    assert inst["app"] == "denpyon"
    assert inst["embed"] == "iframe"
    assert inst["registered_by"] == "sa@example.com"
    definition = json.loads(inst["definition"])
    # 実シークレット値は保存せず参照名のみ（ASSET-01 / §14.2 の契約）。
    assert definition["sso"]["secretRef"] == "denpyon-oidc-client-secret"
    assert "client_secret" not in json.dumps(definition)

    assert uninstall("acme/denpyon", "1.0.0") is True
    assert fake_db.external_app_instances == []
    assert store.find_install("acme/denpyon", "1.0.0") is None


def test_install_sample_app_signature_verified_before_scaffold(fake_db):
    # 署名不正の sample-app は scaffold 展開前に拒否され、ADB に何も書かれない(fail-closed)。
    client, *_ = make_signed_registry([_sample_app_manifest()])
    index = json.loads(client._fetch(INDEX_PATH))
    files = {INDEX_PATH: None}

    # 公開鍵を差し替えて検証を失敗させる。
    other = Ed25519PrivateKey.generate().public_key().public_bytes_raw()
    index["publisherKeys"] = {"acme-key-1": base64.b64encode(other).decode()}
    path = index["plugins"][0]["manifest"]
    files[path] = client._fetch(path)
    files[INDEX_PATH] = json.dumps(index).encode()
    bad_client = RegistryClient(base_url="https://reg/", transport=lambda p: files[p])

    with pytest.raises(SignatureRejected):
        install(bad_client, "acme/crm-lite", "1.0.0", installed_by="sa")
    assert fake_db.sample_app_instances == []
    assert fake_db.sample_app_seed_rows == []
    assert fake_db.installed == []


def test_install_sample_app_rejected_when_host_lacks_capability(fake_db):
    # ホストが必要ケイパビリティ(summarize)を備えない場合、合成不能で取込拒否(fail-closed)。
    client, *_ = make_signed_registry([_sample_app_manifest()])
    with pytest.raises(installer.IngestError):
        install(
            client, "acme/crm-lite", "1.0.0", installed_by="sa",
            available_capabilities=frozenset({"rag.search"}),  # summarize を欠く
        )
    # 取込先・インストール記録のいずれにも残骸を残さない。
    assert fake_db.sample_app_instances == []
    assert fake_db.sample_app_seed_rows == []
    assert fake_db.installed == []


def test_install_sample_app_normalizes_sampleapp_error(fake_db, monkeypatch):
    # 取込側が SampleAppError(構造不正など)を投げても IngestError へ正規化し、残骸を残さない。
    # 検証を迂回構築した manifest でも install/route が 500 にならない(Codex F-001 の防御)。
    from jetuse_core.plugins.sample_app import SampleAppError

    client, *_ = make_signed_registry([_sample_app_manifest()])

    def boom(*a, **k):
        raise SampleAppError("simulated malformed sample-app")

    monkeypatch.setattr(scaffold, "scaffold_sample_app", boom)
    with pytest.raises(installer.IngestError):
        install(client, "acme/crm-lite", "1.0.0", installed_by="sa")
    assert fake_db.sample_app_instances == []
    assert fake_db.installed == []


def test_record_failure_compensation_sample_app(fake_db, monkeypatch):
    # record_install が L2(sample-app)作成後に失敗しても、補償で instance + seed 行が消える
    # (_delete_created の sample_app_instances 分岐の回帰 / Codex review-3 F-001)。
    client, *_ = make_signed_registry([_sample_app_manifest()])

    def boom(*a, **k):
        raise RuntimeError("simulated record_install failure")

    monkeypatch.setattr(installer.store, "record_install", boom)
    with pytest.raises(RuntimeError):
        install(client, "acme/crm-lite", "1.0.0", installed_by="sa")
    assert fake_db.sample_app_instances == []
    assert fake_db.sample_app_seed_rows == []
    assert fake_db.installed == []


def test_record_failure_compensation_connector(fake_db, monkeypatch):
    # record_install が L2(connector)作成後に失敗しても、補償で connector 行が消える
    # (_delete_created の connector_instances 分岐の回帰 / Codex review-3 F-001)。
    client, *_ = make_signed_registry([_connector_manifest()])

    def boom(*a, **k):
        raise RuntimeError("simulated record_install failure")

    monkeypatch.setattr(installer.store, "record_install", boom)
    with pytest.raises(RuntimeError):
        install(client, "acme/slackish", "1.0.0", installed_by="sa")
    assert fake_db.connector_instances == []
    assert fake_db.installed == []


def test_record_failure_compensation_external_app(fake_db, monkeypatch):
    # record_install が L2(external-app)作成後に失敗しても、補償で external-app 行が消える
    # (_delete_created の external_app_instances 分岐の回帰 / BE-06 m-001)。既存行は巻き込まない。
    client, *_ = make_signed_registry([_external_app_manifest()])
    # 同版の別 external-app 行を先に置き、補償が「いま作った行」だけを消すことを確かめる。
    fake_db.external_app_instances.append({
        "id": "preexisting", "plugin_id": "acme/denpyon", "source_version": "1.0.0",
        "name": "old", "app": "denpyon", "embed": "iframe",
        "definition": "{}", "registered_by": "other", "created_at": 0,
    })

    def boom(*a, **k):
        raise RuntimeError("simulated record_install failure")

    monkeypatch.setattr(installer.store, "record_install", boom)
    with pytest.raises(RuntimeError):
        install(client, "acme/denpyon", "1.0.0", installed_by="sa")
    # 既存行は残り、今回作成した行だけが補償削除される。
    assert [r["id"] for r in fake_db.external_app_instances] == ["preexisting"]
    assert fake_db.installed == []


def test_install_connector_rejected_on_undeclared_permission(fake_db):
    # action が要求するスコープが manifest.permissions に宣言されていなければ合成不能で取込拒否。
    md = _connector_manifest(permissions=[], action_perms=["platform:files.read"])
    client, *_ = make_signed_registry([md])
    with pytest.raises(installer.IngestError):
        install(client, "acme/slackish", "1.0.0", installed_by="sa")
    assert fake_db.connector_instances == []
    assert fake_db.installed == []
