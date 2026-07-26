"""AUTH_MODEガード(PORT-02): ~/.oci/config フォールバックの共通ヘルパ。

設定ファイル不在時に未処理のConfigFileNotFoundで落とさず、AUTH_MODE設定漏れの可能性を
明示したRuntimeErrorへ変換することを検証する。ロジックは jetuse_core.oci_auth に集約
（従来 genai にあったものを移設。振る舞いは不変）。
"""

from unittest import mock

import oci
import pytest

from jetuse_core import oci_auth
from jetuse_core.settings import get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    # _mode()/_profile() は settings を参照する（lru_cache）。テスト間の env 差を反映させる。
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_load_local_oci_config_passes_through_on_success(monkeypatch):
    monkeypatch.setattr(oci.config, "from_file", lambda: {"region": "ap-osaka-1"})
    assert oci_auth.load_local_oci_config() == {"region": "ap-osaka-1"}


def test_load_local_oci_config_raises_actionable_error_when_missing():
    with mock.patch.object(
        oci.config, "from_file", side_effect=oci.exceptions.ConfigFileNotFound("~/.oci/config")
    ):
        with pytest.raises(RuntimeError) as ei:
            oci_auth.load_local_oci_config()
    assert "AUTH_MODE" in str(ei.value)
    assert "resource_principal" in str(ei.value)


def test_httpx_auth_wraps_config_file_not_found_from_user_principal_auth(monkeypatch):
    """OciUserPrincipalAuth()は内部でoci.config.from_file()を独自に呼ぶため
    load_local_oci_config()を経由しない。httpx_auth()自体で捕捉して actionable にする。"""
    monkeypatch.delenv("AUTH_MODE", raising=False)

    def boom(*a, **kw):
        raise oci.exceptions.ConfigFileNotFound("~/.oci/config")

    monkeypatch.setattr(oci_auth, "OciUserPrincipalAuth", boom)
    with pytest.raises(RuntimeError) as ei:
        oci_auth.httpx_auth()
    assert "AUTH_MODE" in str(ei.value)
    assert "resource_principal" in str(ei.value)


def test_httpx_auth_leaves_other_errors_untouched(monkeypatch):
    monkeypatch.delenv("AUTH_MODE", raising=False)

    def boom(*a, **kw):
        raise oci.exceptions.InvalidConfig("malformed key")

    monkeypatch.setattr(oci_auth, "OciUserPrincipalAuth", boom)
    with pytest.raises(oci.exceptions.InvalidConfig):
        oci_auth.httpx_auth()
