"""モデルレジストリ(specs/07)。API対応はモデル依存(SPIKE-01実証)。"""

import threading
import time
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

# 利用可否のlazyマーク(PORT-02): 起動時プローブはせず、実際のchat呼び出しが
# NotFound/PermissionDenied(=リージョン/テナンシに無い)で失敗した時点でプロセス内に記録する。
# マーク後は routes/chat.py が実呼び出し自体をスキップするため、TTLで自動的に
# 再試行対象へ戻さないと一時的なIAM伝播遅延・リージョン購読直後の遅延等でも
# プロセス再起動までモデルが永久に使えなくなる(レビュー指摘: 自己回復手段が無い)。
_RETRY_AFTER_SECONDS = 300.0  # ponytail: 固定5分。運用で頻発するなら設定化を検討
_lock = threading.Lock()
_unavailable: dict[str, tuple[str, float]] = {}  # model_key -> (hint, retry_at monotonic)


def mark_unavailable(key: str, hint: str) -> None:
    with _lock:
        _unavailable[key] = (hint, time.monotonic() + _RETRY_AFTER_SECONDS)


def clear_unavailable(key: str | None = None) -> None:
    """テスト用リセット。keyを省略すると全解除。"""
    with _lock:
        if key is None:
            _unavailable.clear()
        else:
            _unavailable.pop(key, None)


def model_status(key: str) -> tuple[bool, str | None]:
    """(利用可能か, 不可の場合のヒント)。TTL経過後は自動的に利用可能へ戻す
    (次回呼び出しで実際に再試行し、まだ不可なら新しいTTLで再マークされる)。

    読み取り専用(GET /api/chat/models・/api/health等からポーリングされうる)なので
    _unavailable への書き込みは行わない(レビュー指摘: 照会が状態を変えるべきではない)。
    期限切れエントリは次にmark_unavailable()が呼ばれた時に上書きされるのみで、放置しても
    MODELSレジストリ規模(数件)を超えて増え続けることはない。
    """
    with _lock:
        entry = _unavailable.get(key)
    if entry is not None and time.monotonic() >= entry[1]:
        entry = None
    if entry is None:
        return True, None
    return False, entry[0]
