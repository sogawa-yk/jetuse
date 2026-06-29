"""OCI Generative AI のテキスト埋め込み(ENH-05)。OpenAI互換APIは /embeddings 非対応
(400 "Unsupported OpenAI operation")のため、ネイティブSDK embed_text を使う。

cohere.embed-multilingual-v3.0(1024次元、日本語対応。Select AI RAGと同一モデル)。
"""

import math
import os
from typing import Any

from .settings import get_settings

EMBED_MODEL = "cohere.embed-multilingual-v3.0"
EMBED_DIM = 1024
_BATCH = 96  # cohereの1リクエスト上限
#: 1 テキストあたり埋め込みに渡す最大文字数。これを超える分は切り詰める(truncate="END")。
#: 呼び出し側(ai_runtime)はこの上限を超える query/文書を semantic 化せず全文評価の lexical へ
#: 回す判断に使う(切り詰めで関連語を取りこぼし false no-hit にしない / BE07-013)。
EMBED_MAX_CHARS = 2000

#: 対話的(同期)用途の既定タイムアウト(connect, read)秒。スロット内 RAG 等、応答待ちが UX に
#: 直結する経路はこの短い境界で呼び、OCI 障害時は SDK 既定(最大8試行/総600秒)を待たずに
#: 早期に例外化 → 呼び出し側(ai_runtime.retrieve)が lexical フォールバックへ即座に退避できる。
#: 対話的経路の retrieval は文書埋め込みを単一バッチに制限し(MAX_SEMANTIC_CORPUS)、埋め込み
#: 呼び出しはクエリ1＋文書1の計2回に有界化される(BE07-007)。さらに **再試行なし(max_attempts=1)**
#: で呼び(`interactive_retry_strategy`)、バックオフ・2回目の connect/read を持たせない。
#: これは絶対 deadline ではなく **best-effort の短い socket タイムアウト**で、SDK 既定(最大8試行/
#: 総600秒)より桁違いに早く失敗して lexical へ退避させるための設定(BE07-016/020)。signer 取得や
#: 継続的に遅い応答など socket タイムアウトで切れない遅延は依然あり得るため、特定秒数は保証しない。
#: クライアント(signer 含む)は timeout 値ごとに再利用し再ハンドシェイクを避ける。
INTERACTIVE_TIMEOUT = (3, 8)

#: timeout 値ごとのクライアント・キャッシュ(None=SDK 既定 timeout)。resource_principal の signer
#: 取得を呼び出し毎に繰り返さないため、同一 timeout のクライアントは再利用する(BE07-020)。
_clients: dict = {}


def _build_client(*, timeout: float | tuple[float, float] | None = None):
    import oci
    from oci.generative_ai_inference import GenerativeAiInferenceClient

    region = get_settings().oci_region
    ep = f"https://inference.generativeai.{region}.oci.oraclecloud.com"
    kwargs: dict[str, Any] = {"service_endpoint": ep}
    if timeout is not None:
        kwargs["timeout"] = timeout
    if os.environ.get("AUTH_MODE") == "resource_principal":
        signer = oci.auth.signers.get_resource_principals_signer()
        return GenerativeAiInferenceClient({"region": region}, signer=signer, **kwargs)
    return GenerativeAiInferenceClient(oci.config.from_file(), **kwargs)


def _embed_client(*, timeout: float | tuple[float, float] | None = None):
    """埋め込みクライアントを返す(timeout 値ごとにプロセス内でキャッシュ・再利用)。

    `timeout` 別に専用クライアントを保持する(既定クライアントの設定を汚さない)。同一 timeout の
    2 回目以降は既存クライアントを返し、resource_principal の signer 取得や TLS ハンドシェイクの
    準備を繰り返さない(対話的 retrieval の待機を増やさない / BE07-020)。
    """
    cli = _clients.get(timeout)
    if cli is None:
        cli = _build_client(timeout=timeout)
        _clients[timeout] = cli
    return cli


def interactive_retry_strategy(*, max_attempts: int = 1, total_seconds: float = 8.0):
    """対話的用途向けの**有界**リトライ戦略。既定は **再試行なし(max_attempts=1)**。

    SDK 既定(最大8試行/総600秒)は同期 UX には長すぎる。対話的 retrieval では再試行による
    バックオフ・追加 connect/read が待機を読めなくする(`total_elapsed_time` は次試行の可否判定で
    あって実行中の試行を打ち切る絶対期限ではない / BE07-016)。よって既定で再試行を行わず、
    1 回の呼び出しを `INTERACTIVE_TIMEOUT` の read で頭打ちにして即座に lexical へ退避する。
    `total_seconds` は max_attempts>1 を明示指定した場合の保険(retry の総経過上限)。

    引数は明示検証する(BE07-019): OCI SDK は falsy な `add_max_attempts(0)`/
    `add_total_elapsed_time(0)` を**無視**して SDK 既定(8試行/600秒)へ戻すため、0/負数を黙って
    受けると「短い境界を指定したつもりが逆に長時間リトライ」になる。これを ValueError で弾く。
    """
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
        raise ValueError("max_attempts は 1 以上の整数でなければならない")
    if (
        isinstance(total_seconds, bool)
        or not isinstance(total_seconds, (int, float))
        or not math.isfinite(total_seconds)
        or total_seconds <= 0
    ):
        raise ValueError("total_seconds は正の有限数でなければならない")
    import oci

    builder = (
        oci.retry.RetryStrategyBuilder()
        .add_max_attempts(max_attempts=max_attempts)
    )
    if max_attempts > 1:
        builder = builder.add_total_elapsed_time(
            total_elapsed_time_seconds=total_seconds
        ).add_service_error_check(
            service_error_retry_config={429: [], 500: [], 502: [], 503: [], 504: []},
            service_error_retry_on_any_5xx=True,
        )
    return builder.get_retry_strategy()


def embed(
    texts: list[str],
    *,
    input_type: str = "SEARCH_DOCUMENT",
    timeout: float | tuple[float, float] | None = None,
    retry_strategy: Any = None,
    truncate: str = "END",
) -> list[list[float]]:
    """テキスト群を埋め込みベクトルに変換する。input_typeは SEARCH_DOCUMENT / SEARCH_QUERY。

    `timeout`(接続/読取秒)と `retry_strategy` を指定すると、対話的経路で OCI 障害時に SDK 既定の
    長いリトライ/タイムアウトを待たず早期に例外化できる(呼び出し側のフォールバックを速める)。
    いずれも未指定時は従来どおり SDK 既定で動く(バッチ/非対話用途は後方互換)。

    `truncate` はモデル上限(cohere は 1 入力 512 トークン)超過時の扱い。既定 "END" は末尾を黙って
    切り詰める(バッチ取込用途の後方互換)。**対話的 retrieval は "NONE" を渡し、512 トークン超を
    OCI 側で例外化させて呼び出し側の lexical フォールバックへ倒す**(BE07-015: 文字数では検知でき
    ない高トークン密度入力を黙って切り詰めて false no-hit にしない)。
    """
    from oci.generative_ai_inference.models import EmbedTextDetails, OnDemandServingMode

    if not texts:
        return []
    out: list[list[float]] = []
    comp = get_settings().compartment_ocid
    cli = _embed_client(timeout=timeout)
    call_kwargs: dict[str, Any] = {}
    if retry_strategy is not None:
        call_kwargs["retry_strategy"] = retry_strategy
    for i in range(0, len(texts), _BATCH):
        # truncate ごとにローカル前処理を分岐する(BE07-022/024)。
        #   NONE  : 切り詰めずサービス側で上限判定(説明と挙動を一致)。
        #   START : 先頭側を捨て **末尾を保持**(t[-N:])。END と同義にしない。
        #   END   : 末尾側を捨て先頭を保持(t[:N]。既定)。
        chunk = texts[i:i + _BATCH]
        if truncate == "NONE":
            batch = chunk
        elif truncate == "START":
            batch = [t[-EMBED_MAX_CHARS:] for t in chunk]
        else:
            batch = [t[:EMBED_MAX_CHARS] for t in chunk]
        det = EmbedTextDetails(
            inputs=batch,
            serving_mode=OnDemandServingMode(model_id=EMBED_MODEL),
            compartment_id=comp,
            truncate=truncate,
            input_type=input_type,
        )
        out.extend(cli.embed_text(det, **call_kwargs).data.embeddings)
    return out
