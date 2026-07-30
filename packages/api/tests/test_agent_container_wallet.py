"""エージェントコンテナ(packages/agent-containers)のウォレット取得(PORT-03)。

公開スタックはウォレットを **base64 テキスト**で Object Storage に置く(`adb_wallet.zip.b64`)。
デコードを忘れると `query_database` ツールだけが BadZipFile で沈黙して落ちるため、
raw ZIP / base64 ZIP / 壊れた入力の3経路をここで固定する(review F-008)。

コンテナのコードは packages/api のパッケージ外にあるので、テスト時だけ import パスへ足す。
"""

import base64
import io
import sys
import types
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agent-containers"))

import agent_db  # noqa: E402


def _wallet_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("tnsnames.ora", "jetusedev_low = (description=...)")
    return buf.getvalue()


@pytest.fixture
def fake_oci(monkeypatch):
    """`import oci` を差し替え、任意のオブジェクト内容を返す Object Storage を装う。"""
    holder: dict = {}

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def get_namespace(self):
            return types.SimpleNamespace(data="ns")

        def get_object(self, ns, bucket, name):
            holder["requested"] = (ns, bucket, name)
            return types.SimpleNamespace(
                data=types.SimpleNamespace(content=holder["content"])
            )

    oci = types.ModuleType("oci")
    oci.object_storage = types.SimpleNamespace(ObjectStorageClient=_Client)
    oci.config = types.SimpleNamespace(from_file=lambda: {})
    monkeypatch.setitem(sys.modules, "oci", oci)
    return holder


@pytest.fixture(autouse=True)
def wallet_env(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_db, "WALLET_CACHE", str(tmp_path / "wallet"))
    monkeypatch.setenv("ADB_WALLET_BUCKET", "app-data")
    monkeypatch.delenv("AUTH_MODE", raising=False)
    monkeypatch.delenv("ADB_WALLET_BASE64", raising=False)
    monkeypatch.delenv("ADB_WALLET_OBJECT", raising=False)


def test_wallet_dir_extracts_raw_zip(fake_oci):
    fake_oci["content"] = _wallet_zip()
    out = Path(agent_db._wallet_dir())
    assert (out / "tnsnames.ora").exists()
    # 既定のオブジェクト名は raw ZIP 側
    assert fake_oci["requested"][2] == "adb_wallet.zip"


def test_wallet_dir_decodes_base64_zip(fake_oci, monkeypatch):
    # 公開スタック(ORM)の配置形式。デコードしないと BadZipFile になる。
    monkeypatch.setenv("ADB_WALLET_BASE64", "true")
    monkeypatch.setenv("ADB_WALLET_OBJECT", "adb_wallet.zip.b64")
    fake_oci["content"] = base64.b64encode(_wallet_zip())
    out = Path(agent_db._wallet_dir())
    assert (out / "tnsnames.ora").exists()
    assert fake_oci["requested"][2] == "adb_wallet.zip.b64"


def test_wallet_dir_fails_loudly_on_corrupt_payload(fake_oci, monkeypatch):
    # 壊れた入力は握りつぶさず例外にする(黙って空のウォレットディレクトリを作らない)。
    monkeypatch.setenv("ADB_WALLET_BASE64", "true")
    fake_oci["content"] = b"not-base64-and-not-a-zip!!"
    with pytest.raises(Exception):  # noqa: B017 - binascii.Error / BadZipFile のどちらでも失敗が正
        agent_db._wallet_dir()
