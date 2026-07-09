"""gen_shared_vault(SP3-09 — ORASEJAPAN 鍵材料の Vault 取得)の契約テスト。

secrets クライアントはモック(実 OCI へ出ない)。検査する契約:
- 取得/解析/署名構成の成功はプロセス生涯キャッシュ(再フェッチしない)
- 失敗は None(fail-closed)+ 短いバックオフの負キャッシュのみ(review-1 M001)。
  バックオフ経過後は再フェッチ = seed 前 placeholder → seed 後に再起動なしで有効化される
- バックオフ中はフェッチ自体を抑止(Vault 障害時のフェッチ嵐防止)
- 鍵材料の値をログへ出さない
- _refresh_signer は Vault から新版を再取得する(ローテーション追従)
"""

import base64
import json
import logging
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from jetuse_core import gen_shared_vault as gsv
from jetuse_core.settings import get_settings

SECRET_OCID = "ocid1.vaultsecret.oc1..gentest"
USER = "ocid1.user.oc1..sharedtestuser"
PEM = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption()).decode()

MATERIAL = {
    "user": USER,
    "tenancy": "ocid1.tenancy.oc1..sharedtest",
    "fingerprint": "aa:bb:cc",
    "region": "ap-osaka-1",
    "key_pem": PEM,
}


def _bundle(payload: bytes, version: int = 1):
    return SimpleNamespace(data=SimpleNamespace(
        version_number=version,
        secret_bundle_content=SimpleNamespace(
            content=base64.b64encode(payload).decode())))


class FakeSecrets:
    def __init__(self, payloads):
        """payloads: 呼び出し順に返す bytes(または raise する Exception)のリスト。"""
        self.payloads = list(payloads)
        self.calls = 0

    def get_secret_bundle(self, secret_id):
        assert secret_id == SECRET_OCID
        self.calls += 1
        p = self.payloads.pop(0)
        if isinstance(p, Exception):
            raise p
        return _bundle(p, version=self.calls)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("GEN_SHARED_SECRET_OCID", SECRET_OCID)
    get_settings.cache_clear()
    monkeypatch.setattr(gsv, "_auth", None)
    monkeypatch.setattr(gsv, "_fail_until", 0.0)
    yield
    get_settings.cache_clear()


def _install(monkeypatch, payloads):
    fake = FakeSecrets(payloads)
    monkeypatch.setattr(gsv, "_secrets_client", lambda: fake)
    return fake


def test_success_builds_signer_and_caches(env, monkeypatch):
    fake = _install(monkeypatch, [json.dumps(MATERIAL).encode()])
    auth = gsv.get_auth()
    assert auth is not None
    assert auth.signer.api_key.startswith(f"{MATERIAL['tenancy']}/{USER}/")
    assert gsv.get_auth() is auth  # プロセス生涯キャッシュ
    assert fake.calls == 1         # 再フェッチしない


def test_unconfigured_returns_none_without_fetch(monkeypatch):
    monkeypatch.delenv("GEN_SHARED_SECRET_OCID", raising=False)
    get_settings.cache_clear()
    monkeypatch.setattr(gsv, "_auth", None)
    fake = _install(monkeypatch, [])
    assert gsv.get_auth() is None
    assert fake.calls == 0
    get_settings.cache_clear()


@pytest.mark.parametrize("payload", [
    b"{}",                                        # seed 前 placeholder
    b"not-json",                                  # 不正 JSON
    b"[1,2]",                                     # 非 object
    json.dumps({**MATERIAL, "key_pem": ""}).encode(),   # 必須キー空
    json.dumps({**MATERIAL, "key_pem": "not-a-pem"}).encode(),  # 署名構成不能
])
def test_bad_material_fails_closed_and_recovers_after_backoff(
        env, monkeypatch, payload, caplog):
    good = json.dumps(MATERIAL).encode()
    fake = _install(monkeypatch, [payload, good])
    with caplog.at_level(logging.WARNING):
        assert gsv.get_auth() is None       # fail-closed
    # バックオフ中はフェッチしない(負キャッシュ — review-1 M001)
    assert gsv.get_auth() is None
    assert fake.calls == 1
    # バックオフ経過後は再フェッチ = seed 後に再起動なしで自然回復
    monkeypatch.setattr(gsv, "_fail_until", 0.0)
    assert gsv.get_auth() is not None
    assert fake.calls == 2
    # 鍵材料の値はログへ出さない
    text = caplog.text
    assert PEM.splitlines()[1] not in text
    assert USER not in text


def test_service_error_fails_closed(env, monkeypatch, caplog):
    _install(monkeypatch, [RuntimeError("service unavailable")])
    with caplog.at_level(logging.WARNING):
        assert gsv.get_auth() is None
    assert "fail-closed" in caplog.text


def test_refresh_signer_is_noop_no_vault_io(env, monkeypatch):
    # 401/周期リフレッシュ経路は Vault へ出ない(イベントループ非閉塞 — review-2 M001)。
    # 材料の張り替えは API 再起動で反映する設計(get_auth の再構築)。
    fake = _install(monkeypatch, [json.dumps(MATERIAL).encode()])
    auth = gsv.get_auth()
    first = auth.signer
    assert fake.calls == 1
    auth._refresh_signer()          # no-op(再フェッチしない・署名据え置き)
    assert fake.calls == 1
    assert auth.signer is first
    # 周期リフレッシュも実質封じられている(_should_refresh_token は当分 False)
    assert not auth._should_refresh_token()


def test_get_auth_single_flight_under_concurrency(env, monkeypatch):
    # コールドスタートで N スレッド同時呼び出しでも Vault フェッチは 1 回(review-2 m002)。
    import threading

    barrier = threading.Barrier(4)
    gate = threading.Event()

    class SlowSecrets:
        def __init__(self):
            self.calls = 0

        def get_secret_bundle(self, secret_id):
            self.calls += 1          # ロック下でのみ入る想定
            gate.wait(2)             # フェッチを引き延ばして重なりを強制
            return _bundle(json.dumps(MATERIAL).encode(), 1)

    fake = SlowSecrets()
    monkeypatch.setattr(gsv, "_secrets_client", lambda: fake)

    results = []

    def worker():
        barrier.wait()
        results.append(gsv.get_auth())

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    gate.set()
    for t in threads:
        t.join(3)

    assert fake.calls == 1                       # single-flight
    assert all(r is not None for r in results)
    assert len({id(r) for r in results}) == 1    # 全員が同一インスタンス
