"""TOOL-02 の検証用フィクスチャ(**架空**の発注 API と、その契約規則)。

顧客データ・顧客名・案件名は持ち込まない。ここにあるのは実在しない品番と、
tasks/TOOL-02.md に記録した**相手 API の契約規則**だけ。

契約規則は「相手が実際に受け取ったヘッダ」に当てて判定する(JetUse の実装は見ない)。
相手(env `TOOL02_ECHO_URL` の公開 https エコー)は規則を強制しないので、**強制の側は
この規則関数が担い、受信内容は実物**という構成にしている(限界は e2e/SKIPPED.md)。
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "ops"))
import _adb as adb  # noqa: E402  環境変数 → .env の順で読む(ops と同じ流儀)

PREFIX = "jetuse-spike-tool02"
SECRET_NAME = f"{PREFIX}-apikey"

# 相手 API が必須とする 3 つ(認証キー / 追跡 ID / 冪等キー)
AUTH_HEADER = "X-Api-Key"
CORRELATION_HEADER = "X-Correlation-Id"
IDEMPOTENCY_HEADER = "X-Idempotency-Key"
CLIENT_HEADER = "X-Client-Version"  # 固定ヘッダを複数持てることの確認用

CORRELATION_VALUE = "corr-2026-08-02-tool02"
CLIENT_VALUE = "jetuse-demo/1.4"

PART_NUMBER = "JX-7742"

# 相手の宛先はコードに焼かない(環境依存値は .env。雛形は .env.example の TOOL02_ECHO_URL)。
# 未設定なら e2e は実行を断る = 承認していない宛先へ検証用トークンを送らない
ECHO_URL = adb.env("TOOL02_ECHO_URL").strip()

ORDER_TOOL = {
    "name": "create_order",
    "description": (
        "社内の発注システムに発注を1件登録する。品番と数量を渡すと受付番号を返す。"
        "発注を頼まれたら必ずこれを使う(社外の情報源では発注できない)"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "part_number": {"type": "string", "description": "品番(例 JX-7742)"},
            "quantity": {"type": "integer", "description": "数量"},
        },
        "required": ["part_number", "quantity"],
    },
    "method": "POST",
}

QUESTION = f"品番 {PART_NUMBER} を 12 個、発注してください。"


def contract_verdict(received: dict, body: str, seen: dict) -> tuple[int, str]:
    """相手 API の契約規則(tasks/TOOL-02.md「実環境で確認した事実」)を当てる。

    `received` は**相手が実際に受け取ったヘッダ**、`seen` は冪等キー→ボディの台帳。
    """
    low = {k.lower(): v for k, v in received.items()}
    if CORRELATION_HEADER.lower() not in low:
        return 400, "MISSING_CORRELATION_ID"
    if IDEMPOTENCY_HEADER.lower() not in low:
        return 400, "MISSING_IDEMPOTENCY_KEY"
    if AUTH_HEADER.lower() not in low:
        return 401, "MISSING_API_KEY"
    key = low[IDEMPOTENCY_HEADER.lower()]
    if key in seen and seen[key] != body:
        return 409, "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_INPUT"
    seen[key] = body
    return 200, "OK"
