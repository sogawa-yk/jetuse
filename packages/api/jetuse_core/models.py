"""モデルレジストリ(specs/07)。API対応はモデル依存(SPIKE-01実証)。"""

from dataclasses import dataclass
from typing import Literal

ApiFamily = Literal["responses", "chat"]


@dataclass(frozen=True)
class ModelDef:
    oci_id: str
    api: ApiFamily
    label: str
    default_temperature: float = 0.7
    reasoning: bool = False  # 推論モデル(reasoning effort対応 — CHAT-04b)
    # max_tokensの実用下限。Gemini系は思考トークンを含むため小さい値だと
    # 本文が空になる/ストリームが返らない(2026-06-11実機。512でも空、2000で正常)
    min_max_tokens: int = 1
    vision: bool = False  # 画像入力対応(MM-01実機確認済みのもののみtrue)
    # 複数画像を1リクエストで受けられるか。llama-3.2-visionは"At most 1 image"で400(ENH-09実機)
    multi_image: bool = False


MODELS: dict[str, ModelDef] = {
    # 標準: agentic対応・TTFT 0.8s
    "gpt-oss-120b": ModelDef(
        "openai.gpt-oss-120b", "responses", "GPT-OSS 120B", reasoning=True
    ),
    # 軽量: TTFT 0.07s。Responsesは404になるためChat Completionsを使う(2026-06-10実機。
    # SPIKE-01時点から挙動変化 — レジストリのapi属性で吸収)
    "llama-3.3-70b": ModelDef("meta.llama-3.3-70b-instruct", "chat", "Llama 3.3 70B"),
    # 高品質: TTFT 10s超 → keepalive必須
    "gemini-2.5-pro": ModelDef(
        "google.gemini-2.5-pro", "chat", "Gemini 2.5 Pro",
        min_max_tokens=2048, vision=True, multi_image=True,
    ),
    "gemini-2.5-flash": ModelDef(
        "google.gemini-2.5-flash", "chat", "Gemini 2.5 Flash",
        min_max_tokens=2048, vision=True, multi_image=True,
    ),
    # 画像対応(MM-01実機確認。command-a-visionは互換APIで404のため不採用)
    "llama-3.2-90b-vision": ModelDef(
        "meta.llama-3.2-90b-vision-instruct", "chat", "Llama 3.2 90B Vision",
        vision=True,
    ),
}

DEFAULT_MODEL = "gpt-oss-120b"
