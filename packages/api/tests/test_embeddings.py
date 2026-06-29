"""OCI 埋め込みラッパ(embeddings.py)の単体テスト(BE-07)。

OCI へ出ずに、対話的経路の有界タイムアウト/リトライ引数がクライアント生成・呼び出しへ
正しく伝播することと、_default_embedder の期待次元(EMBED_DIM)照合・フォールバックを検証する。
"""

import types

import oci.exceptions
import pytest

from jetuse_core import embeddings
from jetuse_core.plugins import ai_runtime


class _FakeEmbedClient:
    def __init__(self, recorder, dim):
        self._recorder = recorder
        self._dim = dim

    def embed_text(self, det, **kwargs):
        self._recorder["call_kwargs"] = kwargs
        self._recorder["inputs"] = list(det.inputs)
        self._recorder["truncate"] = det.truncate
        embs = [[0.1] * self._dim for _ in det.inputs]
        return types.SimpleNamespace(data=types.SimpleNamespace(embeddings=embs))


def test_embed_forwards_timeout_and_retry(monkeypatch):
    """timeout は _embed_client に、retry_strategy は embed_text 呼び出しに伝播する。"""
    rec: dict = {}

    def fake_client(*, timeout=None):
        rec["timeout"] = timeout
        return _FakeEmbedClient(rec, embeddings.EMBED_DIM)

    monkeypatch.setattr(embeddings, "_embed_client", fake_client)
    sentinel = object()
    out = embeddings.embed(
        ["こんにちは"], input_type="SEARCH_QUERY", timeout=(5, 20), retry_strategy=sentinel
    )
    assert rec["timeout"] == (5, 20)
    assert rec["call_kwargs"].get("retry_strategy") is sentinel
    assert len(out) == 1 and len(out[0]) == embeddings.EMBED_DIM


def test_embed_no_retry_kwarg_when_unset(monkeypatch):
    """retry_strategy 未指定時は embed_text に渡さない(SDK 既定動作を維持=後方互換)。"""
    rec: dict = {}

    def fake_client(*, timeout=None):
        return _FakeEmbedClient(rec, embeddings.EMBED_DIM)

    monkeypatch.setattr(embeddings, "_embed_client", fake_client)
    embeddings.embed(["x"])
    assert "retry_strategy" not in rec["call_kwargs"]


def test_embed_truncate_defaults_to_end_and_forwards(monkeypatch):
    """truncate 既定は END(後方互換)、明示指定は EmbedTextDetails へ伝播する(BE07-015)。"""
    rec: dict = {}

    def fake_client(*, timeout=None):
        return _FakeEmbedClient(rec, embeddings.EMBED_DIM)

    monkeypatch.setattr(embeddings, "_embed_client", fake_client)
    embeddings.embed(["x"])
    assert rec["truncate"] == "END"
    embeddings.embed(["x"], truncate="NONE")
    assert rec["truncate"] == "NONE"


def test_default_embedder_uses_truncate_none(monkeypatch):
    """対話的経路は truncate='NONE' で呼ぶ(512トークン超を切り詰めず例外化 → lexical 退避)。"""
    seen: dict = {}

    def spy_embed(texts, *, input_type="SEARCH_DOCUMENT", timeout=None,
                  retry_strategy=None, truncate="END"):
        seen["truncate"] = truncate
        seen["timeout"] = timeout
        return [[0.0] * embeddings.EMBED_DIM for _ in texts]

    monkeypatch.setattr(embeddings, "embed", spy_embed)
    ai_runtime._default_embedder(["a"], "SEARCH_QUERY")
    assert seen["truncate"] == "NONE"
    assert seen["timeout"] == embeddings.INTERACTIVE_TIMEOUT


def test_embed_empty_returns_empty():
    assert embeddings.embed([]) == []


def test_embed_truncates_to_embed_max_chars(monkeypatch):
    """各テキストは EMBED_MAX_CHARS で切り詰めて OCI へ渡す(上限定数が実際に効く)。"""
    rec: dict = {}

    def fake_client(*, timeout=None):
        return _FakeEmbedClient(rec, embeddings.EMBED_DIM)

    monkeypatch.setattr(embeddings, "_embed_client", fake_client)
    embeddings.embed(["x" * (embeddings.EMBED_MAX_CHARS + 500)])
    assert len(rec["inputs"][0]) == embeddings.EMBED_MAX_CHARS


def test_embed_truncate_none_does_not_locally_truncate(monkeypatch):
    """truncate='NONE' はローカル切り詰めせず全文を OCI へ渡す(BE07-022)。"""
    rec: dict = {}

    def fake_client(*, timeout=None):
        return _FakeEmbedClient(rec, embeddings.EMBED_DIM)

    monkeypatch.setattr(embeddings, "_embed_client", fake_client)
    n = embeddings.EMBED_MAX_CHARS + 500
    embeddings.embed(["x" * n], truncate="NONE")
    assert len(rec["inputs"][0]) == n  # 切り詰めない


def test_embed_truncate_end_vs_start_keep_opposite_ends(monkeypatch):
    """END は先頭保持、START は末尾保持(方向を取り違えない / BE07-024)。"""
    rec: dict = {}

    def fake_client(*, timeout=None):
        return _FakeEmbedClient(rec, embeddings.EMBED_DIM)

    monkeypatch.setattr(embeddings, "_embed_client", fake_client)
    text = "H" + "x" * (embeddings.EMBED_MAX_CHARS) + "T"  # 先頭 H / 末尾 T、上限超
    embeddings.embed([text], truncate="END")
    assert rec["inputs"][0][0] == "H" and rec["inputs"][0][-1] != "T"  # 先頭保持
    embeddings.embed([text], truncate="START")
    assert rec["inputs"][0][-1] == "T" and rec["inputs"][0][0] != "H"  # 末尾保持


def test_interactive_retry_strategy_is_bounded():
    """有界リトライ戦略が生成でき、SDK 既定(8試行/600秒)より短い上限を持つ。"""
    rs = embeddings.interactive_retry_strategy(max_attempts=2, total_seconds=30.0)
    assert rs is not None


def test_interactive_retry_strategy_default_is_single_attempt():
    """既定は再試行なし(max_attempts=1)。既定を 2/8 等へ戻す回帰を検出する(BE07-021)。"""
    import inspect

    sig = inspect.signature(embeddings.interactive_retry_strategy)
    assert sig.parameters["max_attempts"].default == 1


def _retryable_503():
    return oci.exceptions.ServiceError(
        status=503, code="ServiceUnavailable", headers={}, message="temporary"
    )


def test_default_strategy_runs_exactly_one_attempt():
    """既定戦略は再試行可能例外でも 1 回で停止する(実挙動の回帰検証 / BE07-023)。"""
    rs = embeddings.interactive_retry_strategy()  # 既定 max_attempts=1
    calls = []

    def boom():
        calls.append(1)
        raise _retryable_503()

    with pytest.raises(oci.exceptions.ServiceError):
        rs.make_retrying_call(boom)
    assert len(calls) == 1


def test_explicit_max_attempts_caps_retries():
    """max_attempts=2 を明示すると再試行可能例外で 2 回まで(=1回再試行)に収まる(BE07-023)。"""
    rs = embeddings.interactive_retry_strategy(max_attempts=2, total_seconds=8.0)
    calls = []

    def boom():
        calls.append(1)
        raise _retryable_503()

    with pytest.raises(oci.exceptions.ServiceError):
        rs.make_retrying_call(boom)
    assert len(calls) == 2


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "2"])
def test_interactive_retry_strategy_rejects_bad_max_attempts(bad):
    """max_attempts の 0/負数/bool/非整数は ValueError(SDK 既定への暗黙降格を防ぐ / BE07-019)。"""
    with pytest.raises(ValueError):
        embeddings.interactive_retry_strategy(max_attempts=bad)


@pytest.mark.parametrize("bad", [0, -5.0, float("inf"), float("nan"), True, "8"])
def test_interactive_retry_strategy_rejects_bad_total_seconds(bad):
    """total_seconds の 0/負/非有限/bool/非数は ValueError(BE07-019)。"""
    with pytest.raises(ValueError):
        embeddings.interactive_retry_strategy(max_attempts=2, total_seconds=bad)


def test_default_embedder_rejects_wrong_dimension(monkeypatch):
    """本番 embedder は EMBED_DIM と異なる次元を破損応答として弾く(retrieve が lexical へ退避)。"""

    def wrong_dim_embed(texts, *, input_type="SEARCH_DOCUMENT", timeout=None,
                        retry_strategy=None, truncate="END"):
        return [[0.1, 0.2, 0.3] for _ in texts]  # 3 次元(EMBED_DIM=1024 でない)

    monkeypatch.setattr(embeddings, "embed", wrong_dim_embed)
    with pytest.raises(ai_runtime._EmbeddingResponseError):
        ai_runtime._default_embedder(["a", "b"], "SEARCH_DOCUMENT")


def test_default_embedder_rejects_count_mismatch(monkeypatch):
    """本番 embedder は件数不一致を破損応答として弾く。"""

    def short_embed(texts, *, input_type="SEARCH_DOCUMENT", timeout=None,
                    retry_strategy=None, truncate="END"):
        return [[0.0] * embeddings.EMBED_DIM]  # 1 件だけ

    monkeypatch.setattr(embeddings, "embed", short_embed)
    with pytest.raises(ai_runtime._EmbeddingResponseError):
        ai_runtime._default_embedder(["a", "b"], "SEARCH_DOCUMENT")


def test_default_embedder_passes_for_correct_shape(monkeypatch):
    """期待件数・期待次元なら通す。"""

    def ok_embed(texts, *, input_type="SEARCH_DOCUMENT", timeout=None,
                 retry_strategy=None, truncate="END"):
        return [[0.0] * embeddings.EMBED_DIM for _ in texts]

    monkeypatch.setattr(embeddings, "embed", ok_embed)
    out = ai_runtime._default_embedder(["a", "b"], "SEARCH_DOCUMENT")
    assert len(out) == 2 and all(len(v) == embeddings.EMBED_DIM for v in out)
