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


def _validate_args(tool: ToolDef, args: dict) -> None:
    props = tool.parameters.get("properties", {})
    for req in tool.parameters.get("required", []):
        if req not in args:
            raise ToolError(f"必須引数がありません: {req}")
    for k, v in args.items():
        if k not in props:
            raise ToolError(f"未知の引数: {k}")
        t = props[k].get("type")
        # 外部HTTPツール(TOOL-01)は利用者定義スキーマなので string 以外も検証する。
        # bool は int の派生なので number/integer からは除く
        if t == "string" and not isinstance(v, str):
            raise ToolError(f"引数の型が不正: {k}")
        if t == "number":
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ToolError(f"引数の型が不正: {k}")
            # NaN / Infinity は JSON の標準にも無く、相手の業務APIへ送る値としても不正。
            # int には isfinite を呼ばない(巨大整数で OverflowError になる)
            if isinstance(v, float) and not math.isfinite(v):
                raise ToolError(f"引数の型が不正: {k}")
        # integer に 1.5 を通すとスキーマの主張と実際の入力保証がずれる
        if t == "integer" and (isinstance(v, bool) or not isinstance(v, int)):
            raise ToolError(f"引数の型が不正: {k}")
        if t == "boolean" and not isinstance(v, bool):
            raise ToolError(f"引数の型が不正: {k}")


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
