"""OCI Generative AI のテキスト埋め込み(ENH-05)。OpenAI互換APIは /embeddings 非対応
(400 "Unsupported OpenAI operation")のため、ネイティブSDK embed_text を使う。

cohere.embed-multilingual-v3.0(1024次元、日本語対応。Select AI RAGと同一モデル)。
"""

import array
import math
from typing import Any

from .settings import get_settings

EMBED_MODEL = "cohere.embed-multilingual-v3.0"
EMBED_DIM = 1024
_BATCH = 96  # cohereの1リクエスト上限

_client = None


def _embed_client():
    global _client
    if _client is None:
        from oci.generative_ai_inference import GenerativeAiInferenceClient

        from .oci_auth import sdk_signer_args

        region = get_settings().oci_region
        ep = f"https://inference.generativeai.{region}.oci.oraclecloud.com"
        _client = GenerativeAiInferenceClient(
            **sdk_signer_args(region), service_endpoint=ep
        )
    return _client


def embed(texts: list[str], *, input_type: str = "SEARCH_DOCUMENT") -> list[list[float]]:
    """テキスト群を埋め込みベクトルに変換する。input_typeは SEARCH_DOCUMENT / SEARCH_QUERY。"""
    from oci.generative_ai_inference.models import EmbedTextDetails, OnDemandServingMode

    if not texts:
        return []
    out: list[list[float]] = []
    comp = get_settings().compartment_ocid
    cli = _embed_client()
    for i in range(0, len(texts), _BATCH):
        batch = [t[:2000] for t in texts[i:i + _BATCH]]
        det = EmbedTextDetails(
            inputs=batch,
            serving_mode=OnDemandServingMode(model_id=EMBED_MODEL),
            compartment_id=comp,
            truncate="END",
            input_type=input_type,
        )
        out.extend(cli.embed_text(det).data.embeddings)
    return out


def as_vector(values: Any) -> array.array:
    """埋め込み応答を `VECTOR` 列へ渡せる形(float32 の配列)にする。**値まで検証する**。

    件数だけを見て中身を見ないと、非数値・NaN/Infinity・次元違いがそのまま
    `array.array("f", ...)` か DB の UPDATE まで届き、**そこで素の例外**(TypeError /
    OverflowError / DPY-xxxx)になる。埋め込みだけ落ちたときは説明を保存して `partial` に
    する、という設計(specs/20 §3)が、上流が壊れた値を返した瞬間に破れる —— 分析全体が
    落ちて、成功していた場面の記述ごと捨てられる。ここで `ValueError` に揃え、
    呼び出し側が「埋め込みが取れなかった」として扱えるようにする(VID-03 レビュー指摘)。

    次元は `EMBED_DIM`(1024)。列は `VECTOR(1024, FLOAT32)`(migration 023)なので、
    次元が違うベクトルは保存できない —— 「近いから入れておく」ができない種類の値。
    """
    if isinstance(values, str) or not isinstance(values, list | tuple | array.array):
        raise ValueError(f"埋め込みが配列ではありません: {type(values).__name__}")
    if len(values) != EMBED_DIM:
        raise ValueError(f"埋め込みの次元が {len(values)} です({EMBED_DIM} 次元が必要)")
    out = array.array("f")
    for i, value in enumerate(values):
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(
                f"埋め込みの {i} 番目が数値ではありません: {type(value).__name__}"
            )
        try:
            finite = math.isfinite(value)
        except OverflowError as e:
            # 巨大な int(10**1000 等)は float への変換自体が落ちる。**ここも
            # ValueError に揃える** —— 素の OverflowError が漏れると、呼び出し側の
            # 「埋め込みだけ失敗」扱いを外れて分析・編集の全体が落ちる
            raise ValueError(
                f"埋め込みの {i} 番目が float32 に収まりません: {str(value)[:40]}…"
            ) from e
        if not finite:
            # NaN / Infinity。**距離計算に載せない** —— 比較が常に偽になる NaN が 1 つ
            # 混じると、その場面は検索で二度と当たらないのに理由が残らない
            raise ValueError(f"埋め込みの {i} 番目が有限の数ではありません: {value}")
        try:
            out.append(value)
        except OverflowError as e:  # int が float に変換できない(桁が大きすぎる)
            raise ValueError(
                f"埋め込みの {i} 番目が float32 に収まりません: {value}"
            ) from e
        if not math.isfinite(out[-1]):
            # **変換した後をもう一度見る。** `array.array("f")` は float32 の範囲を
            # 超える値を**例外ではなく inf に化けさせる**(実測: 1e40 → inf)。
            # 上の isfinite は Python の float を見ているだけなので、ここを通さないと
            # 無限大が列(VECTOR(1024, FLOAT32))に入り、距離が計算できなくなる
            raise ValueError(
                f"埋め込みの {i} 番目が float32 に収まりません: {value}"
            )
    return out
