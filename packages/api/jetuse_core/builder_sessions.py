"""ビルダーセッション リポジトリ(SP3-01 / specs/19 §2.1)。

demos.py の流儀: 所有者強制は SQL の WHERE 句(0 行 = 404 存在秘匿)。owner_sub は
識別列(demos.owner_sub と同じ raw sub — owner_key ヘルパーは通さない)。
状態機械は hearing→designed の 2 状態のみで、生成以降の進行は demo_id と Demo.status
から導出する(状態の二重管理をしない)。demo_id 設定後は読み取り専用 —
書き込みは WHERE の demo_id IS NULL ガードで 0 行にする(ルート側 409)。
"""

import json
import uuid
from typing import Any

from .db import connect

# demo_status は demo_id があるとき JOIN で添える(UI の進行表示用 — specs/19 §2.4)
_SELECT = (
    "SELECT bs.id, bs.status, bs.transcript, bs.requirements, bs.plan, "
    "bs.demo_id, d.status, bs.created_at, bs.updated_at "
    "FROM builder_sessions bs LEFT JOIN demos d ON d.id = bs.demo_id"
)


def _json_col(v: Any) -> Any:
    # 23ai は IS JSON 制約付き CLOB をネイティブ(dict/list)で fetch しうる。文字列版と両対応
    if v is None or isinstance(v, (dict, list)):
        return v
    return json.loads(v)


def _row_to_session(r) -> dict[str, Any]:
    return {
        "id": r[0], "status": r[1],
        "transcript": _json_col(r[2]) or [],
        "requirements": _json_col(r[3]),
        "plan": _json_col(r[4]),
        "demo_id": r[5], "demo_status": r[6],
        "created_at": r[7].isoformat() if r[7] else None,
        "updated_at": r[8].isoformat() if r[8] else None,
    }


def create_session(owner: str) -> dict[str, Any]:
    """status='hearing'・transcript=[] で INSERT(specs/19 §2.4。Body なし)。"""
    sid = str(uuid.uuid4())
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO builder_sessions(id, owner_sub) VALUES (:id, :o)",
            id=sid, o=owner,
        )
        conn.commit()
        cur.execute(f"{_SELECT} WHERE bs.id = :id", id=sid)
        return _row_to_session(cur.fetchone())


def get_session(owner: str, sid: str) -> dict[str, Any] | None:
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            f"{_SELECT} WHERE bs.id = :id AND bs.owner_sub = :o", id=sid, o=owner
        )
        row = cur.fetchone()
        return _row_to_session(row) if row else None


def save_hearing_turn(
    owner: str, sid: str, transcript: list[dict], requirements: dict,
    expected_len: int,
) -> bool:
    """ヒアリング 1 往復の永続化(transcript 全置換 + requirements 上書き)。

    読み→LLM→書きの競合は WHERE で 0 行(False — ルート側 409)にする:
    - demo_id IS NULL: 途中で生成が始まった(読み取り専用化)
    - transcript の JSON 配列長 = :n(読み取り時の件数): 並行 messages の楽観ロック —
      後勝ちの全置換で先行の往復が消えるのを防ぐ(codex review-1 M002)
    allow_nan=False は demos.py と同じ直列化契約(IS JSON)。
    """
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE builder_sessions SET transcript = :t, requirements = :r, "
            "updated_at = SYSTIMESTAMP "
            "WHERE id = :id AND owner_sub = :o AND demo_id IS NULL "
            "AND JSON_VALUE(transcript, '$.size()' RETURNING NUMBER) = :n",
            t=json.dumps(transcript, ensure_ascii=False, allow_nan=False),
            r=json.dumps(requirements, ensure_ascii=False, allow_nan=False),
            id=sid, o=owner, n=expected_len,
        )
        conn.commit()
        return cur.rowcount > 0
