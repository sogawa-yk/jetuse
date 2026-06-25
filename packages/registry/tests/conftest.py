"""レジストリテストの共通フィクスチャ。

実 Object Storage を作らず `InMemoryObjectStore` で publish→index→list/get/download を検証する
(tasks/PLG-04: 統合テストは Object Storage をモック/エミュレートで行う。
実バケット作成は apply=人間ゲート)。
署名ヘルパ・定数は helpers.py に置く。
"""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from helpers import PUBLIC_KEY_ID, PUBLISHER, TOKEN, public_key_b64

from jetuse_registry.publishers import StaticTokenAuthenticator
from jetuse_registry.service import RegistryService
from jetuse_registry.storage import InMemoryObjectStore


@pytest.fixture
def private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture
def store() -> InMemoryObjectStore:
    return InMemoryObjectStore()


@pytest.fixture
def authenticator() -> StaticTokenAuthenticator:
    return StaticTokenAuthenticator.from_token_map({TOKEN: PUBLISHER})


@pytest.fixture
def service(store, authenticator) -> RegistryService:
    return RegistryService(store, authenticator)


@pytest.fixture
def registered_service(service, private_key) -> RegistryService:
    """発行者公開鍵を登録済みのサービス(publish 可能な状態)。"""
    service.register_public_key(TOKEN, PUBLIC_KEY_ID, public_key_b64(private_key))
    return service
