"""モデルレジストリ(specs/07)。API対応はモデル依存(SPIKE-01実証)。

**この登録簿の全項目は us-chicago-1 の実機で測ってある**(AGT-06 / 2026-08-03。
証跡 `docs/verification/AGT-06.md` と `runs/2026-08-03T1125_AGT-06/e2e/probe-*.json`)。
以前は「llama 以外は Responses 非対応」と記録していたが**実機と食い違っていた**
(gemini 系・gpt-oss-20b は Responses で動く)。**測っていないモデル・測っていない
フラグをここに書かないこと。** 「動くはず」は一度この登録簿を誤らせている。

追加するときの手順: `docs/verification/AGT-06.md` の「登録簿を増やすとき」の probe を
そのモデルに対して回し、結果を証跡に足してからここへ書く。
"""

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

    # --- AGT-06: エージェント適性とモデル差 ---
    # エージェントモードで使えない理由(空ならば使える)。吸収しきれない差はここに書き、
    # `model_compat.agent_refusal` が要求時に断る。黙って動かないより断るほうがよい。
    agent_blocked_reason: str = ""
    # `role=system` の入力アイテムを受け付けるか。gemini 系は 400 で拒否する(実測)。
    # false のとき `model_compat.responses_input` が役割だけ user へ移す(本文は変えない)。
    supports_system_role: bool = True
    # 返ってきた `function_call` を **`id` 付きのまま**積み直せるか。gemini 系は
    # stream=True でこれをやると 400 `untagged enum ResponseInput`(実測)。
    # false のとき `id` を落とす(`call_id` は結果の対応付けに要るので残す)。
    echo_call_item_id: bool = True

    @property
    def agent(self) -> bool:
        """エージェントモードで使えるか。理由が無いことと同義にして不整合を作らない。"""
        return not self.agent_blocked_reason


# エージェント不可の定型理由(同じ理由を1か所に置く)
_NO_RESPONSES = "us-chicago-1 では Responses API が 404 を返します(エージェントは Responses 系のみ)"

MODELS: dict[str, ModelDef] = {
    # --- エージェント可(Responses + 入れ子引数つき function call を実測) ---
    # 標準: agentic対応・TTFT 0.8s
    "gpt-oss-120b": ModelDef(
        "openai.gpt-oss-120b", "responses", "GPT-OSS 120B", reasoning=True
    ),
    # 軽量。以前は未登録だったが Responses で動く(関数呼び出しも可 — 実測)
    "gpt-oss-20b": ModelDef(
        "openai.gpt-oss-20b", "responses", "GPT-OSS 20B", reasoning=True
    ),
    # Grok 系(大阪には無い)。ADR-0001 の「Grok 不可」は**大阪の話**でシカゴには当てはまらない。
    # grok-4.3 だけが reasoning effort を受け付ける(4.20 系は 400 "does not support
    # parameter reasoningEffort" — 実測)
    "grok-4.3": ModelDef(
        "xai.grok-4.3", "responses", "Grok 4.3",
        reasoning=True, vision=True, multi_image=True,
    ),
    "grok-4.20-reasoning": ModelDef(
        "xai.grok-4.20-reasoning", "responses", "Grok 4.20 Reasoning",
        vision=True, multi_image=True,
    ),
    "grok-4.20-non-reasoning": ModelDef(
        "xai.grok-4.20-non-reasoning", "responses", "Grok 4.20",
        vision=True, multi_image=True,
    ),
    # Gemini 系。**以前は api="chat" と記録していたが誤り**で、Responses で動く(実測)。
    # ただし system ロールを拒否し、function_call を id 付きで積み直せない
    # → model_compat が吸収する。reasoning effort は 400(thinking_level 非対応)。
    "gemini-2.5-pro": ModelDef(
        "google.gemini-2.5-pro", "responses", "Gemini 2.5 Pro",
        min_max_tokens=2048, vision=True, multi_image=True,
        supports_system_role=False, echo_call_item_id=False,
    ),
    "gemini-2.5-flash": ModelDef(
        "google.gemini-2.5-flash", "responses", "Gemini 2.5 Flash",
        min_max_tokens=2048, vision=True, multi_image=True,
        supports_system_role=False, echo_call_item_id=False,
    ),
    "gemini-2.5-flash-lite": ModelDef(
        "google.gemini-2.5-flash-lite", "responses", "Gemini 2.5 Flash Lite",
        min_max_tokens=2048, vision=True, multi_image=True,
        supports_system_role=False, echo_call_item_id=False,
    ),

    # --- エージェント不可(理由つきで断る) ---
    # Responses 単体・画像入力は動くが、client-side tools が beta 限定で 400 になる(実測)。
    # ツールを渡せない = エージェントの前提が成り立たないので、要求時に断る。
    "grok-4.20-multi-agent": ModelDef(
        "xai.grok-4.20-multi-agent", "responses", "Grok 4.20 Multi-Agent",
        agent_blocked_reason=(
            "このモデルは client-side tools が beta アクセス限定で、"
            "ツール付きの呼び出しが 400 になります"
        ),
    ),
    # Chat Completions のみ。Responses は 404(2026-06-10 大阪 / 2026-08-03 シカゴとも)
    "llama-3.3-70b": ModelDef(
        "meta.llama-3.3-70b-instruct", "chat", "Llama 3.3 70B",
        agent_blocked_reason=_NO_RESPONSES,
    ),
    # 画像対応(MM-01実機確認。command-a-visionは互換APIで404のため不採用)。
    # 複数画像は "At most 1 image" で 400(ENH-09。シカゴでも再確認済み)。
    # **注意: シカゴでは非推奨日 2026-05-15 を既に過ぎている**(登録簿のうちこれだけ)。
    # 現時点では応答するが、後継の選定が要る(docs/verification/AGT-06.md §5)
    "llama-3.2-90b-vision": ModelDef(
        "meta.llama-3.2-90b-vision-instruct", "chat", "Llama 3.2 90B Vision",
        vision=True, agent_blocked_reason=_NO_RESPONSES,
    ),
}

# 既定モデル。**変更は人間ゲート**。2026-08-03 に `gpt-oss-120b` から変更(ADR-0027 §5-A)。
# 完了率では上位 7 モデルに差が無く(docs/comparison/agent-capable-models.md §4)、
# 争点は速度・画像対応・可用性だった。grok-4.3 は完了 3/3・平均 5.0 秒・**画像対応**・
# reasoning effort 対応。**シカゴにしか無い**が、大阪は B-1(完全撤去)と決まったため
# 「大阪にも在る既定を保つ」理由が無くなった。gpt-oss-120b は一覧に残り要求で選べる。
DEFAULT_MODEL = "grok-4.3"

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
