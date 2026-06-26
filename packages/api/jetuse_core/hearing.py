"""ヒアリングセッション/回答/推薦のリポジトリ(HBD-01)。

§7 データモデル(hearing_session / hearing_answer / recommendation)を正として CLOB に保存する。
所有権は SQL(owner_sub)で強制し、回答・推薦は所有セッションにのみ書ける。回答値は
`hearing_schema.validate_answer` を必ず通してから保存する(未知選択肢・型不一致を DB へ入れない)。
推薦の決定は `recommend.recommend`(決定ルール)に委ね、本モジュールは永続のみを担う。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import oracledb

from .db import connect
from .hearing_schema import (
    ANSWER_SOURCES,
    MAX_INPUT_NOTES_CHARS,
    SESSION_STATUSES,
    HearingSchemaError,
    validate_answer,
)
from .recommend import Recommendation


def _uid() -> str:
    return str(uuid.uuid4())


def _clob(value: Any) -> str:
    return value.read() if hasattr(value, "read") else value


def _clob_inputsizes(cur, *names: str) -> None:
    """指定バインド名を CLOB として明示する(>4000 byte の JSON/メモを VARCHAR2 境界で壊さない)。

    oracledb は str を既定で VARCHAR2 バインドするため、CLOB 列へ長文を入れると ORA-01461/12899 に
    なりうる。CLOB 列(input_notes / value / ai_parts / connectors / validation / detail)へ入れる
    バインドだけを CLOB に固定する。
    """
    cur.setinputsizes(**{n: oracledb.DB_TYPE_CLOB for n in names})


def _bound_notes(input_notes: str | None) -> str | None:
    """input_notes を上限(MAX_INPUT_NOTES_CHARS)で検証する。超過は HearingSchemaError。"""
    if input_notes is None:
        return None
    if len(input_notes) > MAX_INPUT_NOTES_CHARS:
        raise HearingSchemaError(
            f"input_notes が上限 {MAX_INPUT_NOTES_CHARS} 文字を超える"
        )
    return input_notes


# --- セッション -------------------------------------------------------------


def create_session(owner: str, input_notes: str | None = None) -> dict[str, Any]:
    sid = _uid()
    notes = _bound_notes(input_notes)
    with connect() as conn:
        cur = conn.cursor()
        _clob_inputsizes(cur, "notes")
        cur.execute(
            """
            INSERT INTO hearing_session(id, owner_sub, status, input_notes)
            VALUES (:id, :o, 'draft', :notes)
            """,
            id=sid, o=owner, notes=notes,
        )
        conn.commit()
    return {"id": sid, "owner_sub": owner, "status": "draft", "input_notes": notes}


def list_sessions(owner: str) -> list[dict[str, Any]]:
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, status, created_at, updated_at
            FROM hearing_session
            WHERE owner_sub = :o
            ORDER BY updated_at DESC
            FETCH FIRST 200 ROWS ONLY
            """,
            o=owner,
        )
        return [
            {
                "id": r[0], "status": r[1],
                "created_at": r[2].isoformat() if r[2] else None,
                "updated_at": r[3].isoformat() if r[3] else None,
            }
            for r in cur.fetchall()
        ]


def _owns_session(cur, owner: str, session_id: str) -> bool:
    cur.execute(
        "SELECT 1 FROM hearing_session WHERE id = :id AND owner_sub = :o",
        id=session_id, o=owner,
    )
    return cur.fetchone() is not None


def get_session(owner: str, session_id: str) -> dict[str, Any] | None:
    """セッション本体＋回答一覧＋(あれば)推薦を返す。所有者でなければ None。"""
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, owner_sub, status, input_notes, created_at, updated_at
            FROM hearing_session WHERE id = :id AND owner_sub = :o
            """,
            id=session_id, o=owner,
        )
        row = cur.fetchone()
        if not row:
            return None
        session = {
            "id": row[0], "owner_sub": row[1], "status": row[2],
            "input_notes": _clob(row[3]),
            "created_at": row[4].isoformat() if row[4] else None,
            "updated_at": row[5].isoformat() if row[5] else None,
            "answers": _answers_for(cur, session_id),
            "recommendation": _recommendation_for(cur, session_id),
        }
        return session


def _answers_for(cur, session_id: str) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT question_id, value, source, updated_at
        FROM hearing_answer WHERE session_id = :s ORDER BY question_id
        """,
        s=session_id,
    )
    return [
        {
            "question_id": r[0], "value": json.loads(_clob(r[1])), "source": r[2],
            "updated_at": r[3].isoformat() if r[3] else None,
        }
        for r in cur.fetchall()
    ]


def update_session(
    owner: str,
    session_id: str,
    *,
    status: str | None = None,
    input_notes: str | None = None,
) -> dict[str, Any] | None:
    """status / input_notes を更新する。所有者でなければ None。両方 None でも updated_at は進む。"""
    notes = _bound_notes(input_notes)
    sets = ["updated_at = SYSTIMESTAMP"]
    binds: dict[str, Any] = {"id": session_id, "o": owner}
    if status is not None:
        if status not in SESSION_STATUSES:
            raise HearingSchemaError(
                f"未知の status: {status!r}(候補: {sorted(SESSION_STATUSES)})"
            )
        # 'confirmed' は推薦確定ゲート(confirm_recommendation)だけが付与する。汎用 PATCH からの
        # 直接遷移を拒否し、推薦不在のまま確定済みにする迂回を塞ぐ。
        if status == "confirmed":
            raise HearingSchemaError(
                "status='confirmed' は recommend/confirm 経由でのみ設定できる"
            )
        sets.append("status = :st")
        binds["st"] = status
    if input_notes is not None:
        sets.append("input_notes = :notes")
        binds["notes"] = notes
    with connect() as conn:
        cur = conn.cursor()
        if "notes" in binds:
            _clob_inputsizes(cur, "notes")
        cur.execute(
            f"UPDATE hearing_session SET {', '.join(sets)} "
            "WHERE id = :id AND owner_sub = :o",
            binds,
        )
        if cur.rowcount == 0:
            return None
        conn.commit()
    return get_session(owner, session_id)


def delete_session(owner: str, session_id: str) -> bool:
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM hearing_session WHERE id = :id AND owner_sub = :o",
            id=session_id, o=owner,
        )
        conn.commit()
        return cur.rowcount > 0


# --- 回答(upsert) ----------------------------------------------------------


def save_answer(
    owner: str,
    session_id: str,
    question_id: str,
    value: Any,
    *,
    source: str = "sa",
) -> dict[str, Any] | None:
    """回答 1 件を保存(差し替え)する。所有者でなければ None。値は質問スキーマで検証する。

    `source` は 'sa' | 'genai_suggested'(§7)。同一 (session, question) は upsert で 1 行に保つ。
    """
    if source not in ANSWER_SOURCES:
        raise HearingSchemaError(f"未知の source: {source!r}(候補: {sorted(ANSWER_SOURCES)})")
    normalized = validate_answer(question_id, value)  # 未知選択肢/型不一致は弾く
    payload = json.dumps(normalized, ensure_ascii=False)
    with connect() as conn:
        cur = conn.cursor()
        if not _owns_session(cur, owner, session_id):
            return None
        _clob_inputsizes(cur, "v")
        cur.execute(
            """
            UPDATE hearing_answer
            SET value = :v, source = :src, updated_at = SYSTIMESTAMP
            WHERE session_id = :s AND question_id = :q
            """,
            v=payload, src=source, s=session_id, q=question_id,
        )
        if cur.rowcount == 0:
            _clob_inputsizes(cur, "v")
            try:
                cur.execute(
                    """
                    INSERT INTO hearing_answer(id, session_id, question_id, value, source)
                    VALUES (:id, :s, :q, :v, :src)
                    """,
                    id=_uid(), s=session_id, q=question_id, v=payload, src=source,
                )
            except oracledb.IntegrityError:
                # 並行初回保存の競合(UQ_HEARING_ANSWER)。先に入った行を UPDATE で上書きする
                # (upsert を 500 にせず冪等にする)。
                _clob_inputsizes(cur, "v")
                cur.execute(
                    """
                    UPDATE hearing_answer
                    SET value = :v, source = :src, updated_at = SYSTIMESTAMP
                    WHERE session_id = :s AND question_id = :q
                    """,
                    v=payload, src=source, s=session_id, q=question_id,
                )
        # セッションの更新時刻を進め、確定済みなら回答変更で 'ready' へ戻す(status/推薦の整合)。
        # CLOB サイズ指定はここで解除する。
        cur.setinputsizes()
        cur.execute(
            "UPDATE hearing_session SET updated_at = SYSTIMESTAMP, "
            "status = CASE WHEN status = 'confirmed' THEN 'ready' ELSE status END "
            "WHERE id = :s",
            s=session_id,
        )
        # 回答が変わると既存の推薦は陳腐化する。古い行を残すと再推薦なしに confirm され得るため、
        # **推薦行ごと削除**する(SA は再 recommend してから confirm する)。confirm は推薦不在を
        # not_found として弾く=陳腐化した推薦の再確定を構造的に不能にする。
        cur.execute("DELETE FROM recommendation WHERE session_id = :s", s=session_id)
        # 推薦が陳腐化したら、それを基に起動した demo_launch も陳腐化する。古い起動記録を残すと
        # GET /launch が陳腐な構成を返し続けるため**起動記録も削除**する(回答変更で起動は無効化)。
        cur.execute("DELETE FROM demo_launch WHERE session_id = :s", s=session_id)
        conn.commit()
    return {"question_id": question_id, "value": normalized, "source": source}


def get_answers(owner: str, session_id: str) -> dict[str, Any] | None:
    """回答を {question_id: value} の辞書で返す(recommend 入力用)。所有者でなければ None。"""
    with connect() as conn:
        cur = conn.cursor()
        if not _owns_session(cur, owner, session_id):
            return None
        return {a["question_id"]: a["value"] for a in _answers_for(cur, session_id)}


# --- 推薦(upsert) ----------------------------------------------------------


def _recommendation_for(cur, session_id: str) -> dict[str, Any] | None:
    cur.execute(
        """
        SELECT detail, confirmed_at FROM recommendation WHERE session_id = :s
        """,
        s=session_id,
    )
    row = cur.fetchone()
    if not row:
        return None
    detail = json.loads(_clob(row[0]))
    detail["confirmed_at"] = row[1].isoformat() if row[1] else None
    return detail


def save_recommendation(
    owner: str, session_id: str, rec: Recommendation
) -> dict[str, Any] | None:
    """決定ルールが返した推薦を保存(差し替え)する。所有者でなければ None。"""
    detail = rec.model_dump()
    detail_json = json.dumps(detail, ensure_ascii=False)
    with connect() as conn:
        cur = conn.cursor()
        if not _owns_session(cur, owner, session_id):
            return None
        binds = {
            "s": session_id, "app": rec.sample_app,
            "parts": json.dumps(rec.ai_parts, ensure_ascii=False),
            "conn": json.dumps(rec.connectors, ensure_ascii=False),
            "ui": rec.ui, "seed": rec.seed_strategy,
            "val": rec.validation.model_dump_json(),
            "detail": detail_json,
        }
        _clob_inputsizes(cur, "parts", "conn", "val", "detail")
        cur.execute(
            """
            UPDATE recommendation
            SET sample_app = :app, ai_parts = :parts, connectors = :conn,
                ui = :ui, seed_strategy = :seed, validation = :val, detail = :detail,
                confirmed_at = NULL
            WHERE session_id = :s
            """,
            binds,
        )
        if cur.rowcount == 0:
            _clob_inputsizes(cur, "parts", "conn", "val", "detail")
            try:
                cur.execute(
                    """
                    INSERT INTO recommendation(id, session_id, sample_app, ai_parts,
                                               connectors, ui, seed_strategy, validation, detail)
                    VALUES (:id, :s, :app, :parts, :conn, :ui, :seed, :val, :detail)
                    """,
                    {**binds, "id": _uid()},
                )
            except oracledb.IntegrityError:
                # 並行初回保存の競合(uq_recommendation_session)。先に入った行を UPDATE で上書きする
                # (save_answer と同じく upsert を 500 にせず冪等にする)。
                _clob_inputsizes(cur, "parts", "conn", "val", "detail")
                cur.execute(
                    """
                    UPDATE recommendation
                    SET sample_app = :app, ai_parts = :parts, connectors = :conn,
                        ui = :ui, seed_strategy = :seed, validation = :val, detail = :detail,
                        confirmed_at = NULL
                    WHERE session_id = :s
                    """,
                    binds,
                )
        # 推薦が確定したのでセッション status を整える: 未着手(draft)は ready へ進め、確定済み
        # (confirmed)は差し替えで未確定へ戻す。いずれも「推薦あり・未確定」の ready に揃える。
        cur.setinputsizes()
        cur.execute(
            "UPDATE hearing_session SET status = 'ready', updated_at = SYSTIMESTAMP "
            "WHERE id = :s AND status IN ('draft', 'confirmed')",
            s=session_id,
        )
        # 推薦を差し替えると confirmed_at は NULL に戻る(未確定)。既存の起動記録は旧推薦に基づく
        # 陳腐な構成なので削除する(再確定→再起動するまで GET /launch を 404 に戻す)。
        cur.execute("DELETE FROM demo_launch WHERE session_id = :s", s=session_id)
        conn.commit()
    return {**detail, "confirmed_at": None}


# --- デモ起動記録(HBD-05) --------------------------------------------------


def _launch_to_record(row) -> dict[str, Any]:
    return {
        "id": row[0],
        "session_id": row[1],
        "sample_app": row[2],
        "instance_id": row[3],
        "entry_slot": row[4],
        "demo_url": row[5],
        "composition": json.loads(_clob(row[6])),
        "status": row[7],
        "launched_at": row[8].isoformat() if row[8] else None,
    }


_LAUNCH_COLS = (
    "id, session_id, sample_app, instance_id, entry_slot, demo_url, "
    "composition, status, launched_at"
)


def record_launch(
    owner: str,
    session_id: str,
    *,
    sample_app: str,
    instance_id: str,
    entry_slot: str | None,
    demo_url: str,
    composition: dict[str, Any],
) -> dict[str, Any] | None:
    """デモ起動を記録(差し替え)する。所有者でなければ None。1 セッション 1 起動(upsert)。

    呼び出し側(ルート)が合成＋ガバナンス検証 PASS を確認してから呼ぶ前提。本関数は永続のみ。
    """
    comp_json = json.dumps(composition, ensure_ascii=False)
    with connect() as conn:
        cur = conn.cursor()
        if not _owns_session(cur, owner, session_id):
            return None
        # 所有者境界を UPDATE 条件にも明示する(demo_launch は owner_sub を持つ)。先頭の
        # _owns_session で確認済みだが、万一 session_id が再利用/衝突しても他オーナーの起動記録を
        # 上書きしない多層防御にする(F-003)。
        binds = {
            "s": session_id, "o": owner, "app": sample_app, "inst": instance_id,
            "slot": entry_slot, "url": demo_url, "comp": comp_json,
        }
        _clob_inputsizes(cur, "comp")
        cur.execute(
            """
            UPDATE demo_launch
            SET sample_app = :app, instance_id = :inst, entry_slot = :slot,
                demo_url = :url, composition = :comp, status = 'launched',
                launched_at = SYSTIMESTAMP
            WHERE session_id = :s AND owner_sub = :o
            """,
            binds,
        )
        if cur.rowcount == 0:
            _clob_inputsizes(cur, "comp")
            try:
                cur.execute(
                    """
                    INSERT INTO demo_launch(id, session_id, owner_sub, sample_app,
                                            instance_id, entry_slot, demo_url, composition)
                    VALUES (:id, :s, :o, :app, :inst, :slot, :url, :comp)
                    """,
                    {**binds, "id": _uid()},
                )
            except oracledb.IntegrityError:
                # 並行初回起動の競合(uq_demo_launch_session)。先に入った行を上書きする(冪等)。
                _clob_inputsizes(cur, "comp")
                cur.execute(
                    """
                    UPDATE demo_launch
                    SET sample_app = :app, instance_id = :inst, entry_slot = :slot,
                        demo_url = :url, composition = :comp, status = 'launched',
                        launched_at = SYSTIMESTAMP
                    WHERE session_id = :s AND owner_sub = :o
                    """,
                    binds,
                )
        conn.commit()
    return get_launch(owner, session_id)


def get_launch(owner: str, session_id: str) -> dict[str, Any] | None:
    """セッションの起動記録を返す。所有者でなければ/未起動なら None。"""
    with connect() as conn:
        cur = conn.cursor()
        if not _owns_session(cur, owner, session_id):
            return None
        cur.execute(
            f"SELECT {_LAUNCH_COLS} FROM demo_launch WHERE session_id = :s",
            s=session_id,
        )
        row = cur.fetchone()
        return _launch_to_record(row) if row else None


def confirm_recommendation(owner: str, session_id: str) -> str:
    """推薦を SA 確定する。戻り値: 'confirmed' | 'not_found' | 'unresolved'。

    - 所有セッション＋推薦が存在し `sample_app` が確定済みのときだけ確定する(confirmed_at を
      スタンプし、セッション status を 'confirmed' へ遷移)。
    - 推薦が無い/他人 → 'not_found'。`sample_app` が NULL(Q1=other で主SBA未確定) → 'unresolved'
      (最近傍を反映し再推薦するまで確定させない=未解決のブラックボックス確定を防ぐ)。
    """
    with connect() as conn:
        cur = conn.cursor()
        if not _owns_session(cur, owner, session_id):
            return "not_found"
        cur.execute(
            "SELECT sample_app FROM recommendation WHERE session_id = :s", s=session_id
        )
        row = cur.fetchone()
        if not row:
            return "not_found"
        if row[0] is None:
            return "unresolved"
        cur.execute(
            "UPDATE recommendation SET confirmed_at = SYSTIMESTAMP WHERE session_id = :s",
            s=session_id,
        )
        cur.execute(
            "UPDATE hearing_session SET status = 'confirmed', updated_at = SYSTIMESTAMP "
            "WHERE id = :s",
            s=session_id,
        )
        conn.commit()
        return "confirmed"
