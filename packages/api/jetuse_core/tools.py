"""エージェントツールレジストリ(AGT-01)。

サーバー側でのみ実行。execute-toolはレジストリ名+JSON Schema検証済み引数のみ受理。
web_search built-inはOCI不可(SPIKE-09)のためDuckDuckGo HTMLで自前実装。
"""

import json
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass

# web_search / web_fetch / SSRFガード / DDGパーサ / get_current_time は jetuse_shared へ一本化(P1b)
# 本モジュールは Responses API のツールレジストリ(JSON Schema・承認要否)に専念し、
# 実装は jetuse_shared へ委譲する薄い adapter。
from jetuse_shared import webtools as _wt
from jetuse_shared.webtools import (
    SEARCH_RESULTS,
    SEARCH_TIMEOUT,
    _DdgParser,  # noqa: F401  後方互換の再エクスポート
)

logger = logging.getLogger("jetuse.tools")

# 実行時の配列要素数の上限(TOOL-03・ADR-0024)。宣言時ではなく**呼び出しごと**に効く。
# 業務 API の配列(明細行・サービス項目)はデモの操作で数件〜十数件。100 件はその十倍で、
# 「モデルが暴走して巨大な配列を組み立てた」を相手へ送る前に止めるための天井。
# 超過は切り詰めずに失敗させる(切り詰めると相手には成功に見えて中身が欠ける)
MAX_ARRAY_ITEMS = 100


def web_search_handler(args: dict) -> str:
    return _wt.web_search_json(
        args["query"], max_results=SEARCH_RESULTS, timeout=SEARCH_TIMEOUT
    )


def get_current_time_handler(args: dict) -> str:
    return _wt.get_current_time_json()


def query_database_handler(args: dict) -> str:
    """NL2SQL(SQL Search)→読取専用実行(SQL-02のガード再利用)。生成に30秒程度"""
    from . import nl2sql

    question = args["question"]
    sql = nl2sql.generate_sql(question)
    result = nl2sql.execute_readonly(sql)
    return json.dumps({
        "sql": sql,
        "columns": result["columns"],
        "rows": result["rows"][:20],
        "row_count": result["row_count"],
        "truncated": result["truncated"] or result["row_count"] > 20,
    }, ensure_ascii=False)


def web_fetch_handler(args: dict) -> str:
    # ツール出力は 8000字上限(jetuse_shared.web_fetch 既定 MAX_TEXT_CHARS=8000)。SSRFも共有側
    return _wt.web_fetch_json(args["url"])


@dataclass(frozen=True)
class ToolDef:
    name: str
    label: str
    description: str
    parameters: dict
    handler: Callable[[dict], str] | None  # Noneはbuilt-in(OCI側実行)
    requires_approval: bool = True
    # 登録済み外部HTTPツール(TOOL-01)の id。組込ツールは空。
    # 承認往復で「承認したその1件」を名前でなく id で名指しするために使う
    tool_id: str = ""


TOOLS: dict[str, ToolDef] = {
    "web_search": ToolDef(
        name="web_search",
        label="Web検索",
        description="Webを検索して上位の結果(タイトル・URL・抜粋)を返す。最新情報や事実確認に使う",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string", "description": "検索クエリ"}},
            "required": ["query"],
        },
        handler=web_search_handler,
    ),
    "web_fetch": ToolDef(
        name="web_fetch",
        label="Webページ取得",
        description="指定URLのページ本文を取得する。web_searchで見つけたURLの内容を読むのに使う",
        parameters={
            "type": "object",
            "properties": {"url": {"type": "string", "description": "取得するURL"}},
            "required": ["url"],
        },
        handler=web_fetch_handler,
    ),
    "query_database": ToolDef(
        name="query_database",
        label="データベース照会",
        description="データベース(販売データ)に自然言語で質問しSQLを自動生成・実行して結果を返す。"
        "売上・顧客・商品などの数値質問に使う。実行に30秒程度かかる",
        parameters={
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "データベースへの質問(日本語可)"}
            },
            "required": ["question"],
        },
        handler=query_database_handler,
        requires_approval=False,  # 読取専用ユーザー+SELECT限定ガード済み(SQL-02)
    ),
    "get_current_time": ToolDef(
        name="get_current_time",
        label="現在日時",
        description="現在の日本時間(日付・時刻・曜日)を返す。「今日」「今週」等の質問の前に使う",
        parameters={"type": "object", "properties": {}},
        handler=get_current_time_handler,
        requires_approval=False,
    ),
}

RAG_SEARCH = "rag_search"  # 実体はfile_search built-in(ユーザーのVector Store) — AGT-01c


CODE_INTERPRETER = "code_interpreter"


def list_tools() -> list[dict]:
    """UIのツール選択リスト用(AGT-01b/01c)"""
    items = [
        {"name": t.name, "label": t.label, "description": t.description, "builtin": False}
        for t in TOOLS.values()
    ]
    items.append({
        "name": CODE_INTERPRETER,
        "label": "コード実行",
        "description": "Pythonコードをサンドボックスで実行して計算・分析する(OCI側で実行)",
        "builtin": True,
    })
    items.append({
        "name": RAG_SEARCH,
        "label": "文書検索(RAG)",
        "description": "アップロード済み文書から関連箇所を検索して回答の根拠にする"
        "(文書未登録時は無効)",
        "builtin": True,
    })
    return items


def tool_specs(enabled: list[str] | None = None) -> list[dict]:
    """Responses APIのtools配列。enabled指定時はその名前のみ(AGT-01b)"""
    specs: list[dict] = [
        {
            "type": "function",
            "name": t.name,
            "description": t.description,
            "parameters": t.parameters,
        }
        for t in TOOLS.values()
        if enabled is None or t.name in enabled
    ]
    if enabled is None or CODE_INTERPRETER in enabled:
        specs.append({"type": "code_interpreter", "container": {"type": "auto"}})
    return specs


class ToolError(ValueError):
    pass


SCALAR_TYPES = ("string", "number", "integer", "boolean")


def _validate_scalar(t: str | None, v, path: str) -> None:
    if t not in SCALAR_TYPES:
        # 宣言できる型は object/array + 上記スカラだけ(`http_tools.ALLOWED_PARAM_TYPES`)。
        # ここに来るのは type の欠落・未知の型 = DB を直接書き換えられた印なので、
        # 「検証できないものは通さない」に倒す(素通しすると未検証値が相手の業務APIへ飛ぶ)
        raise ToolError(f"引数スキーマが不正です(検証できない type): {path}")
    # 外部HTTPツール(TOOL-01)は利用者定義スキーマなので string 以外も検証する。
    # bool は int の派生なので number/integer からは除く
    if t == "string" and not isinstance(v, str):
        raise ToolError(f"引数の型が不正: {path}")
    if t == "number":
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ToolError(f"引数の型が不正: {path}")
        # NaN / Infinity は JSON の標準にも無く、相手の業務APIへ送る値としても不正。
        # int には isfinite を呼ばない(巨大整数で OverflowError になる)
        if isinstance(v, float) and not math.isfinite(v):
            raise ToolError(f"引数の型が不正: {path}")
    # integer に 1.5 を通すとスキーマの主張と実際の入力保証がずれる
    if t == "integer" and (isinstance(v, bool) or not isinstance(v, int)):
        raise ToolError(f"引数の型が不正: {path}")
    if t == "boolean" and not isinstance(v, bool):
        raise ToolError(f"引数の型が不正: {path}")


def _validate_object(spec: dict, value: dict, path: str) -> None:
    """object の中身を検査する。未知キー拒否・required は**各階層で**同じ強さで効かせる。"""
    props = spec.get("properties", {})
    required = spec.get("required", [])
    if not isinstance(props, dict) or not isinstance(required, list):
        # 宣言側(`http_tools.validate_parameters`)を通ればこの形になる。ならないのは
        # DB を直接書き換えられた印なので、検証できないまま実行しない
        raise ToolError(f"引数スキーマが不正です(properties/required): {path or 'parameters'}")
    for req in required:
        if not isinstance(req, str):
            # 文字列でない required は dict の照合で TypeError(unhashable)になる。
            # 500 ではなく「拒否」として返す(ADR-0023 §_load_headers と同じ扱い)
            raise ToolError(f"引数スキーマが不正です(required): {path or 'parameters'}")
        if req not in value:
            raise ToolError(f"必須引数がありません: {f'{path}.{req}' if path else req}")
    for k, v in value.items():
        if k not in props:
            raise ToolError(f"未知の引数: {f'{path}.{k}' if path else k}")
        _validate_value(props[k], v, f"{path}.{k}" if path else k)


def _validate_value(spec: dict, value, path: str) -> None:
    """宣言された1ノードに対して実際の入力を検査する(TOOL-03: 入れ子・配列を再帰で)。

    宣言(`http_tools.validate_parameters`)と同じ強さで検査する。宣言側が
    「検証しきれる形しか通さない」ので、ここに `properties` の無い object や
    `items` の無い array は来ない。来たら(DB を直接書き換えられた等)**通さない**。
    """
    if not isinstance(spec, dict):
        # 子スキーマが dict でない(文字列・配列・null)。宣言側を通ればこうはならないので
        # DB を直接書き換えられた印。AttributeError で 500 にせず、拒否として返す
        raise ToolError(f"引数スキーマが不正です: {path or 'parameters'}")
    t = spec.get("type")
    if t == "object":
        if not isinstance(value, dict):
            raise ToolError(f"引数の型が不正: {path}")
        _validate_object(spec, value, path)
    elif t == "array":
        if not isinstance(value, list):
            raise ToolError(f"引数の型が不正: {path}")
        if len(value) > MAX_ARRAY_ITEMS:
            # 切り詰めない。切り詰めると相手の業務APIへ「送ったつもりより少ない」注文が届く
            raise ToolError(
                f"配列 {path} の要素数が上限({MAX_ARRAY_ITEMS}件)を超えています: {len(value)}件"
            )
        items = spec.get("items")
        if not isinstance(items, dict):
            raise ToolError(f"引数スキーマが不正です(items がありません): {path}")
        for i, v in enumerate(value):
            _validate_value(items, v, f"{path}[{i}]")
    else:
        _validate_scalar(t, value, path)


def _validate_args(tool: ToolDef, args: dict) -> None:
    # ルートも子と**同じ強さ**で検査する。ここを `_validate_object` へ直接渡すと、
    # PARAMETERS を `type=array` / 未知 type / type 欠落 へ書き換えられたときに
    # properties さえ整っていれば handler へ到達する(子だけ fail-closed にしても意味がない)
    if not isinstance(tool.parameters, dict) or tool.parameters.get("type") != "object":
        raise ToolError("引数スキーマが不正です: parameters")
    _validate_object(tool.parameters, args, "")


def execute_with(registry: dict[str, ToolDef], name: str, arguments: str | dict) -> str:
    """指定レジストリのツールを検証付きで実行する(AGT-01ガード)。

    エージェント実行では組込 `TOOLS` に owner の外部HTTPツール(TOOL-01)を重ねた
    「そのターン限りのレジストリ」を渡す。
    """
    tool = registry.get(name)
    if not tool or tool.handler is None:
        raise ToolError(f"未知のツール: {name}")
    args = json.loads(arguments) if isinstance(arguments, str) else arguments
    if not isinstance(args, dict):
        raise ToolError("引数はJSONオブジェクトである必要があります")
    _validate_args(tool, args)
    try:
        return tool.handler(args)
    except ToolError:
        raise
    except Exception as e:
        logger.exception("tool execution failed: %s", name)
        return json.dumps(
            {"error": f"ツール実行に失敗しました: {str(e)[:200]}"}, ensure_ascii=False
        )


def execute_tool(name: str, arguments: str | dict) -> str:
    """組込レジストリ(TOOLS)のツールを実行する。"""
    return execute_with(TOOLS, name, arguments)
