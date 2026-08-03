"""Responses API のモデル差を吸収する層(AGT-06)。

同じ Responses API でも、受け付ける**入力アイテムの形**がモデルで違う。
実測(us-chicago-1 / docs/verification/AGT-06.md)で確認した差は 2 つだけで、
ここではその 2 つだけを吸収する(推測で先回りしない)。

1. `role=system` の入力アイテム — gemini 系は 400 で拒否する。
   → 役割だけ `user` へ移す。**本文は変えない**(指示の内容を書き換えると、
     どのモデルで何が起きたのか比較できなくなる)。
2. 返ってきた `function_call` アイテムを `id` 付きで積み直すこと — gemini 系は
   stream=True のとき 400 `did not match any variant of untagged enum ResponseInput`。
   → `id` を落とす。`call_id` は `function_call_output` との対応付けに要るので残す。

吸収できない差(client-side tools 非対応など)は登録簿の `agent_blocked_reason` に持たせ、
`agent_refusal()` が要求時に断る。黙って動かないより断るほうがよい。
"""

from .models import MODELS, ModelDef


def responses_input(model: ModelDef, items: list[dict]) -> list[dict]:
    """Responses API の `input` をモデルに合わせて整える。

    差が無いモデルでは入力をそのまま返す(現行モデルの挙動を変えない)。
    入力は往復のたびに使い回されるので**破壊的に書き換えない**。
    """
    if model.supports_system_role and model.echo_call_item_id:
        return items
    out: list[dict] = []
    for item in items:
        if (
            not model.supports_system_role
            and item.get("type") == "message"
            and item.get("role") == "system"
        ):
            out.append({**item, "role": "user"})
        elif not model.echo_call_item_id and item.get("type") == "function_call":
            out.append({k: v for k, v in item.items() if k != "id"})
        else:
            out.append(item)
    return out


def agent_refusal(model_key: str) -> str | None:
    """エージェントモードで使えないなら理由を返す。使えるなら None。

    「動くはずだが動かない」を黙って通さないための関門。理由はそのまま
    利用者へ出す文言なので、どのモデルの何が理由かが分かる形にする。
    """
    model = MODELS.get(model_key)
    if model is None:
        return f"モデル {model_key} は登録されていません"
    if model.agent:
        return None
    if model.agent_blocked_reason:
        return f"{model.label} はエージェントモードでは使えません: {model.agent_blocked_reason}"
    return f"{model.label} はエージェントモードでは使えません"
