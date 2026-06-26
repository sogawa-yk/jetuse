"""結果表→グラフ仕様(ChartSpec)の提案コア(SQL-03)。API/組込フレームワーク共有 — jetuse_shared。

`jetuse_core/nl2sql.py::suggest_chart`(DBチャット /api/dbchat/chart)と
`jetuse_core/plugins/ai_runtime.py` の `chart` capability ハンドラ(sample-app 組込)が
同一の提案・検証ロジックを共有するための純粋関数。LLM 呼び出しは差し替え可能な `generate`
コールバックで注入するため、DB/openai 依存を持たず単体テストが外部に出ない。

返り値 ChartSpec: {type: bar|line|pie|none, x, y, title, reason}。LLM 出力は JSON で受け、
列名の実在チェックで検証する(存在しない列を指す提案は none に落とす)。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

#: 提案できるグラフ種別。none は「グラフ化に不適」。
CHART_TYPES = ("bar", "line", "pie", "none")


def _none(reason: str) -> dict[str, Any]:
    return {"type": "none", "x": None, "y": [], "title": "", "reason": reason}


def propose_chart(
    generate: Callable[[str], str],
    question: str,
    columns: list[str],
    rows: list[list[str]],
) -> dict[str, Any]:
    """結果表に最適なグラフを LLM(`generate`)へ提案させ、検証して ChartSpec を返す。

    `generate(prompt) -> raw` は呼び出し側がモデル/接続を束ねたコールバック。提案が解析不能・
    未対応種別・存在しない列を指す場合は type="none" にフォールバックする(成功偽装しない)。
    """
    if not columns or not rows:
        return _none("グラフ化できるデータがありません")
    sample = "\n".join(",".join(r) for r in rows[:15])
    prompt = (
        "あなたはデータ可視化アシスタントです。以下のSQL実行結果に最適なグラフを"
        "JSONだけで提案してください。説明文は不要です。\n"
        f'形式: {{"type": "bar|line|pie|none", "x": "X軸の列名", '
        f'"y": ["数値列名"], "title": "グラフタイトル(日本語)", "reason": "選定理由(短く)"}}\n'
        "ルール: 時系列はline、カテゴリ比較はbar、構成比(5件程度まで)はpie、"
        'グラフ化に不適(数値列がない等)なら {"type": "none", "reason": "..."}。\n\n'
        f"元の質問: {question}\n列: {', '.join(columns)}\n"
        f"データ(先頭{min(len(rows), 15)}行):\n{sample}"
    )
    raw = generate(prompt)
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return _none("提案の解析に失敗しました")
    try:
        spec = json.loads(m.group(0))
    except json.JSONDecodeError:
        return _none("提案の解析に失敗しました")
    if spec.get("type") not in CHART_TYPES:
        return _none("未対応のグラフ種別が提案されました")
    if spec["type"] != "none":
        if spec.get("x") not in columns:
            return _none("提案されたX軸列が結果に存在しません")
        ys = [c for c in (spec.get("y") or []) if c in columns]
        if not ys:
            return _none("提案された数値列が結果に存在しません")
        spec["y"] = ys
    return {
        "type": spec["type"],
        "x": spec.get("x"),
        "y": spec.get("y", []),
        "title": str(spec.get("title") or "")[:100],
        "reason": str(spec.get("reason") or "")[:200],
    }
