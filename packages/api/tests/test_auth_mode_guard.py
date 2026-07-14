"""AUTH_MODEガード(PORT-02): ~/.oci/config フォールバックの共通ヘルパ。

設定ファイル不在時に未処理のConfigFileNotFoundで落とさず、AUTH_MODE設定漏れの可能性を
明示したRuntimeErrorへ変換することを検証する(genai/obs/tts/stt_realtime/docunderstand/
minutes/guardrails/embeddings/mcp_servers/agents/rag/translate/dbが共通で使う)。
"""

from unittest import mock

import oci
import pytest

from jetuse_core import genai


def test_load_local_oci_config_passes_through_on_success(monkeypatch):
    monkeypatch.setattr(oci.config, "from_file", lambda: {"region": "ap-osaka-1"})
    assert genai.load_local_oci_config() == {"region": "ap-osaka-1"}


def test_load_local_oci_config_raises_actionable_error_when_missing():
    with mock.patch.object(
        oci.config, "from_file", side_effect=oci.exceptions.ConfigFileNotFound("~/.oci/config")
    ):
        with pytest.raises(RuntimeError) as ei:
            genai.load_local_oci_config()
    assert "AUTH_MODE" in str(ei.value)
    assert "resource_principal" in str(ei.value)


def test_signer_wraps_config_file_not_found_from_user_principal_auth(monkeypatch):
    """PORT-02レビュー指摘: OciUserPrincipalAuth()は内部でoci.config.from_file()を
    独自に呼ぶため、load_local_oci_config()を経由しない。make_inference_client/
    make_cp_client/nl2sqlなどgenai系全経路の入口である_signer()自体で捕捉する。"""
    monkeypatch.delenv("AUTH_MODE", raising=False)

    def boom(*a, **kw):
        raise oci.exceptions.ConfigFileNotFound("~/.oci/config")

    monkeypatch.setattr(genai, "OciUserPrincipalAuth", boom)
    with pytest.raises(RuntimeError) as ei:
        genai._signer()
    assert "AUTH_MODE" in str(ei.value)
    assert "resource_principal" in str(ei.value)


def test_signer_leaves_other_errors_from_user_principal_auth_untouched(monkeypatch):
    monkeypatch.delenv("AUTH_MODE", raising=False)

    def boom(*a, **kw):
        raise oci.exceptions.InvalidConfig("malformed key")

    monkeypatch.setattr(genai, "OciUserPrincipalAuth", boom)
    with pytest.raises(oci.exceptions.InvalidConfig):
        genai._signer()
