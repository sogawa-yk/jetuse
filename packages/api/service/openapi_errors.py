"""エラー応答の仕様(API-01)。

**なぜ要るか**: この API の主な利用者はコーディングエージェントである。人は 400 を食らって
から試行錯誤できるが、エージェントは**仕様に無いことを推測(でっち上げ)する**。エラーから
自己修正させるには「どういうときに返るか」と「応答の形」が仕様に載っていなければならない。

FastAPI が自動で載せるのは 422 だけなので、他のコードはここで**1 か所**に定義し、各ルートは
`responses=error_responses(...)` で参照する(ルートごとに文言を書き写さない = ズレない)。
"""

from pydantic import BaseModel, Field

# 422 以外の 4xx/5xx はすべてこの形(FastAPI の HTTPException の既定形)。
_DETAIL_DESC = (
    "何が問題かを述べる人間可読の文。エージェントはこの文をそのまま手掛かりにして"
    "リクエストを直せる(例: `agent and rag cannot be combined` なら `rag` を外して"
    "`enabled_tools` に `rag_search` を入れる)。"
)


class ErrorResponse(BaseModel):
    """エラー応答の共通形。

    **422 だけは別形**(`HTTPValidationError` = `detail` が項目ごとの配列)。
    """

    detail: str = Field(
        description=_DETAIL_DESC,
        examples=["agent and rag cannot be combined"],
    )


# 「どういうときに返るか」と「どう直すか」。ルート固有の事情は overrides で足す。
_MEANINGS: dict[int, str] = {
    400: "**リクエストの組み合わせが不正**（値は型としては正しいが、一緒には使えない・"
         "前提のパラメータが足りない・存在しないモデル名など）。再送しても同じ結果になるので、"
         "`detail` を読んでパラメータを直してから呼び直す。",
    401: "**認証が必要**（トークンが無い / 期限切れ / 署名や issuer/audience が合わない）。"
         "`AUTH_REQUIRED=true` の配備でだけ起きる（開発用の `false` 配備では認証不要）。"
         "この API に**トークン発行の口は無い**ので、配備側から受け取ったトークンを "
         "`authorization: Bearer <token>` で送る。",
    404: "**参照した資源が無い**（未作成・削除済み・他人の資源）。所有者が違う場合も 404 にする"
         "（存在の有無を漏らさないため）。id を作り直す／一覧を引き直す。",
    409: "**状態が合わない**（前段の処理が終わっていない・承認した対象が変わっている）。"
         "リクエスト自体は正しいので、前段をやり直す・状態を確認してから再送する。",
    413: "**サイズ上限超過**。黙って切り詰めずに断る。分割するか小さくして送り直す。",
    422: "**入力の検証エラー**（必須欠落・型違い・列挙値外・上限超過・入力の組み合わせが不正）。"
         "**このコードだけ `detail` が 2 通りある**: スキーマ検証で落ちた場合は"
         "項目ごとの**配列**(`loc`=どのフィールドか / `msg`=理由 / `type`=種別)、"
         "ルート側の検証で落ちた場合は他のコードと同じ**文字列**"
         "（例: `images cannot be combined with agent/rag` / `empty file`）。"
         "**両方を受けられるように書くこと**（`detail` が配列なら `loc` を見て直す）。",
    502: "**上流のサービスがエラーを返した**（OCI Generative AI 等。権限不足・未整備は 503 側に"
         "分けてある）。リクエスト自体は正しいので、`detail` のヒントを見て設定を確認し、"
         "一時的な失敗なら再試行してよい。",
    503: "**依存サービスが一時的に使えない**（DB 停止・OCI 側の障害・索引の準備中など）。"
         "リクエストは正しいので、時間を置いて同じ内容で再試行してよい。",
}


ERROR_REF = "#/components/schemas/ErrorResponse"
VALIDATION_REF = "#/components/schemas/HTTPValidationError"


def _json(description: str, schema: str | dict) -> dict:
    """`application/json` の応答を**明示**で作る。

    `model=` に任せると、FastAPI はそのルートの `response_class` の media type を使う。
    SSE ルート(`response_class=SSEResponse`)ではエラー応答まで `text/event-stream` と
    宣言されてしまう(実際は JSON)ので、ここで media type を固定する。
    """
    if isinstance(schema, str):
        schema = {"$ref": schema}
    return {"description": description, "content": {"application/json": {"schema": schema}}}


def error_responses_as_model(*codes: int) -> dict[int, dict]:
    """`model=` 形で返す（**JSON を返すルート限定**）。

    `$ref` だけでは `ErrorResponse` が `components/schemas` に登録されない（FastAPI は
    `model=` で使われたモデルを集める）。仕様の入口である `/api/openapi.json` の 401 を
    この形で宣言し、**登録の錨**を 1 か所に固定する。SSE ルートでこれを使うと media type が
    `text/event-stream` になってしまうので使わない（`error_responses()` を使う）。
    """
    return {code: {"description": _MEANINGS[code], "model": ErrorResponse} for code in codes}


def error_responses(*codes: int, **overrides: str) -> dict[int, dict]:
    """OpenAPI の `responses` 断片を作る。

    `overrides` は「このルートでは具体的にこう返る」を足すためのもの（キーはコードの文字列）。
    例: `error_responses(400, 404, **{"400": "`agent` と `rag` の併用など…"})`
    """
    out: dict[int, dict] = {}
    for code in codes:
        description = _MEANINGS[code]
        extra = overrides.get(str(code))
        if extra:
            description = f"{description}\n\nこのルートでの具体例: {extra}"
        if code == 401:
            # 401 は全 router へ共通で付ける（下記 _json 参照）
            out[code] = _json(description, ERROR_REF)
        elif code == 422:
            # 422 は**実装が 2 通り返す**（review-6 F-001）。スキーマ検証で落ちれば FastAPI 生成の
            # HTTPValidationError（項目ごとの配列）、ルート側の検証（画像の組み合わせ・拡張子・
            # 空ファイル等）で落ちれば HTTPException なので ErrorResponse（文字列）。
            # **片方だけを載せると生成クライアントが実応答を解けない**ので oneOf で両方載せる。
            # model= に渡せる Pydantic モデルにできないため content を直接書く（description だけ
            # 足して content を省くと既定の content ごと消える）。
            out[code] = _json(
                description,
                {"oneOf": [{"$ref": VALIDATION_REF}, {"$ref": ERROR_REF}]},
            )
        else:
            out[code] = _json(description, ERROR_REF)
    return out
