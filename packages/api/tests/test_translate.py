"""翻訳(ENH-10)の方式選択とフォールバックの単体テスト。"""

from unittest import mock

from jetuse_core import translate


def test_empty_text_returns_empty():
    assert translate.translate("", "en") == ""
    assert translate.translate("   ", "en", backend="oci_language") == ""


def test_llm_backend_is_default():
    with mock.patch.object(translate, "_via_llm", return_value="hello") as m:
        assert translate.translate("こんにちは", "en") == "hello"
        m.assert_called_once()


def test_oci_language_backend_used_when_selected():
    with mock.patch.object(translate, "_via_oci_language", return_value="hello") as ml, \
            mock.patch.object(translate, "_via_llm") as mllm:
        assert translate.translate("こんにちは", "en", backend="oci_language") == "hello"
        ml.assert_called_once()
        mllm.assert_not_called()


def test_oci_language_failure_falls_back_to_llm():
    # CIのRPに use ai-service-language-family 未付与時(404)等でもLLMで翻訳継続
    with mock.patch.object(translate, "_via_oci_language", side_effect=Exception("404")), \
            mock.patch.object(translate, "_via_llm", return_value="hello") as mllm:
        assert translate.translate("こんにちは", "en", backend="oci_language") == "hello"
        mllm.assert_called_once()
