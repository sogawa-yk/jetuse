"""Select AI Agent(ADB 26ai DBネイティブ・エージェント)実行(ENH-04 / SPIKE-E1)。

ADR-0009のhosted SDKコンテナ群と並列の「第4のエージェント実行種別」。
DBMS_CLOUD_AI_AGENT で選択ツール付きエージェント/タスク/チームを構築し、会話を設定して
RUN_TEAM で実行する。NL→SQL/RAGはDB内(Select AIプロファイル)で完結する。

ツール: Select AI Agent組込のうち本アプリで配線可能な SQL / RAG を提示・選択させる
(NOTIFICATION=通知設定要 / WEBSEARCH=資格情報要 のため対象外)。

前提: GRANT EXECUTE ON DBMS_CLOUD_AI_AGENT TO JETUSE_APP(ops/setup-select-ai.py)。
実機確定のAPI/属性は docs/verification/SPIKE-E1.md / docs/tips.md 参照。
"""

import hashlib
import json
import logging

import oracledb

from .db import connect

logger = logging.getLogger("jetuse.select_ai_agent")

DEFAULT_PROFILE = "JETUSE_SQL_AI"  # SHサンプル(SQLツール用)
DEFAULT_ROLE = (
    "あなたはデータベースのアナリストです。SQLで根拠を確認し、日本語で簡潔に答えます。"
)
ROW_GUARD = (
    " 一覧系の質問では結果を最大50行に制限し(必要なら上位のみ・FETCH FIRST 50 ROWS ONLY)、"
    "全件の生データは返さないこと。集計・合計・件数は通常どおりでよい。"
)

# UI提示用カタログ(Select AI Agentで利用可能なツール)
SELECT_AI_TOOLS = [
    {"name": "sql", "label": "DB照会（SQL/NL2SQL）",
     "description": "DBに自然言語で質問しSQLを生成・実行して答える(Select AI SQLツール)"},
    {"name": "rag", "label": "文書検索（RAG）",
     "description": "アップロード済み文書をベクトル検索して根拠付きで答える(Select AI RAGツール)"},
]
VALID_TOOLS = {t["name"] for t in SELECT_AI_TOOLS}


class PayloadTooLargeError(RuntimeError):
    """結果が大きすぎてLLMに渡せない/エージェント内部ジョブ失敗(HTTP 413等)"""


def _base(owner: str, agent_id: str) -> str:
    return "SAI_" + hashlib.sha1(f"{owner}:{agent_id}".encode()).hexdigest()[:8].upper()


def _names(owner: str, agent_id: str, role: str, tools: list[str]) -> dict[str, str]:
    base = _base(owner, agent_id)
    sig = hashlib.sha1((role + "|" + ",".join(sorted(tools))).encode()).hexdigest()[:6].upper()
    return {"base": base, "tool_sql": f"{base}_TLS", "tool_rag": f"{base}_TLR",
            "agent": f"{base}_AG_{sig}", "task": f"{base}_TK_{sig}", "team": f"{base}_TM_{sig}"}


def _exists(cur, view: str, col: str, name: str) -> bool:
    cur.execute(f"SELECT COUNT(*) FROM {view} WHERE {col} = :n", n=name)
    return cur.fetchone()[0] > 0


def _ensure_tool(cur, name: str, attrs: dict) -> None:
    if _exists(cur, "user_ai_agent_tools", "tool_name", name):
        return
    cur.execute(
        "BEGIN DBMS_CLOUD_AI_AGENT.CREATE_TOOL(tool_name=>:t, attributes=>:a); END;",
        t=name, a=json.dumps(attrs),
    )


def _ensure(cur, n: dict, sql_profile: str, rag_profile: str | None,
            role: str, tools: list[str]) -> None:
    if _exists(cur, "user_ai_agent_teams", "agent_team_name", n["team"]):
        return
    tool_list: list[str] = []
    if "sql" in tools:
        _ensure_tool(cur, n["tool_sql"],
                     {"tool_type": "SQL", "tool_params": {"profile_name": sql_profile}})
        tool_list.append(n["tool_sql"])
    if "rag" in tools and rag_profile:
        _ensure_tool(cur, n["tool_rag"],
                     {"tool_type": "RAG", "tool_params": {"profile_name": rag_profile}})
        tool_list.append(n["tool_rag"])
    if not tool_list:  # 最低1ツール(SQL)
        _ensure_tool(cur, n["tool_sql"],
                     {"tool_type": "SQL", "tool_params": {"profile_name": sql_profile}})
        tool_list = [n["tool_sql"]]
    cur.execute(
        "BEGIN DBMS_CLOUD_AI_AGENT.CREATE_AGENT(agent_name=>:g, attributes=>:a); END;",
        g=n["agent"],
        a=json.dumps({"profile_name": sql_profile,
                      "role": (role[:1500] or DEFAULT_ROLE) + ROW_GUARD}),
    )
    cur.execute(
        "BEGIN DBMS_CLOUD_AI_AGENT.CREATE_TASK(task_name=>:k, attributes=>:a); END;",
        k=n["task"], a=json.dumps({"instruction": "{query}", "tools": tool_list}),
    )
    cur.execute(
        "BEGIN DBMS_CLOUD_AI_AGENT.CREATE_TEAM(team_name=>:m, attributes=>:a); END;",
        m=n["team"],
        a=json.dumps({"agents": [{"name": n["agent"], "task": n["task"]}],
                      "process": "sequential"}),
    )


def run(owner: str, agent_id: str, question: str, *,
        role: str = DEFAULT_ROLE, tools: list[str] | None = None,
        sql_profile: str = DEFAULT_PROFILE) -> str:
    """Select AI Agent を実行して回答テキストを返す(選択ツールでチーム構築→会話→RUN_TEAM)。"""
    tools = [t for t in (tools or []) if t in VALID_TOOLS] or ["sql"]
    rag_profile = None
    if "rag" in tools:
        from . import rag_select_ai
        try:
            rag_profile = rag_select_ai.ensure_profile(owner)
        except Exception:  # noqa: BLE001
            logger.warning("rag profile unavailable; skipping RAG tool")
            tools = [t for t in tools if t != "rag"] or ["sql"]
    n = _names(owner, agent_id, role, tools)
    with connect() as conn:
        # RUN_TEAM はLLMでNL→SQL→実行を多段で行うため既定10sでは足りない(DPY-4024)
        conn.call_timeout = 240_000
        cur = conn.cursor()
        _ensure(cur, n, sql_profile, rag_profile, role, tools)
        conn.commit()
        cid = cur.var(oracledb.DB_TYPE_VARCHAR)
        cur.execute("BEGIN :id := DBMS_CLOUD_AI.CREATE_CONVERSATION(); END;", id=cid)
        conv = cid.getvalue()
        try:
            cur.execute("BEGIN DBMS_CLOUD_AI.SET_CONVERSATION_ID(:id); END;", id=conv)
            cur.execute(
                f"SELECT DBMS_CLOUD_AI_AGENT.RUN_TEAM(team_name=>'{n['team']}', "
                "user_prompt=>:q) FROM dual",
                q=question[:2000],
            )
            out = cur.fetchone()[0]
            return out.read() if hasattr(out, "read") else (out or "")
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if "413" in msg or "ORA-20413" in msg:
                raise PayloadTooLargeError(
                    "結果が大きすぎてエージェントが処理できませんでした。"
                    "集計や条件で絞って質問してください(例: 上位N件・合計・期間指定)。"
                ) from e
            if "ORA-20053" in msg:
                logger.warning("select_ai agent job failed: %s", msg[:300])
                raise PayloadTooLargeError(
                    "エージェントの処理に失敗しました。質問をより具体的に、"
                    "または集計・上位N件の形に言い換えて再度お試しください。"
                ) from e
            raise
        finally:
            try:
                cur.execute("BEGIN DBMS_CLOUD_AI.DROP_CONVERSATION(:id); END;", id=conv)
                conn.commit()
            except Exception:  # noqa: BLE001
                pass


def drop(owner: str, agent_id: str) -> None:
    """エージェント削除時、本人の当該agentに紐づくSelect AIオブジェクトを前方一致で後始末。"""
    base = _base(owner, agent_id)
    plan = [
        ("user_ai_agent_teams", "agent_team_name", "DROP_TEAM", "team_name"),
        ("user_ai_agent_tasks", "task_name", "DROP_TASK", "task_name"),
        ("user_ai_agents", "agent_name", "DROP_AGENT", "agent_name"),
        ("user_ai_agent_tools", "tool_name", "DROP_TOOL", "tool_name"),
    ]
    with connect() as conn:
        cur = conn.cursor()
        for view, col, proc, arg in plan:
            try:
                cur.execute(f"SELECT {col} FROM {view} WHERE {col} LIKE :p", p=f"{base}%")
            except Exception:  # noqa: BLE001
                continue
            for (nm,) in cur.fetchall():
                try:
                    cur.execute(f"BEGIN DBMS_CLOUD_AI_AGENT.{proc}({arg}=>'{nm}'); END;")
                    conn.commit()
                except Exception:  # noqa: BLE001
                    pass
