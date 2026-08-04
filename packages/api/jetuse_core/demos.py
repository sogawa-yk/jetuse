"""Demo エンティティ(SP1-02 最小形 → SP2-01 完全形 / specs/18 §1.3)。

usecases.py の流儀: 所有者強制は SQL の WHERE 句。get_demo は owner_sub / status を含む
生取得で、認可判定は呼び出し側(require_demo)。全 UPDATE 文で updated_at = SYSTIMESTAMP。
status は API から直接変更不可(サーバ管理列)で、遷移は set_status の楽観 UPDATE のみ。
"""

import json
import uuid
from typing import Any

from .db import connect

_COLS = "id, owner_sub, name, description, visibility, status, config, created_at, updated_at"
# UPDATE 文を組み立ててよい列(信頼境界: SQL に混ぜるキーは allowlist のみ)
_UPDATABLE = ("name", "description", "visibility", "config")


def _row_to_demo(r) -> dict[str, Any]:
    # 23ai は IS JSON 制約付き CLOB をネイティブ JSON(dict)で fetch する。文字列版と両対応
    config = r[6] if isinstance(r[6], dict) else json.loads(r[6] or "{}")
    return {
        "id": r[0], "owner_sub": r[1], "name": r[2], "description": r[3],
        "visibility": r[4], "status": r[5], "config": config,
        "created_at": r[7].isoformat() if r[7] else None,
        "updated_at": r[8].isoformat() if r[8] else None,
    }


def create_demo(
    owner: str,
    name: str,
    description: str | None = None,
    visibility: str = "private",
    config: dict[str, Any] | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """INSERT のみ。status 省略時は DB DEFAULT='ready'。外部リソースは作らない(specs/18 §3.1)。

    status='provisioning' はビルダー内部作成(§4.5)用 — 完了で set_status(→ready/failed)。
    """
    demo_id = str(uuid.uuid4())
    cols = "id, owner_sub, name, description, visibility, config"
    vals = ":id, :o, :n, :descr, :v, :c"
    binds: dict[str, Any] = {
        "id": demo_id, "o": owner, "n": name[:200],
        "descr": description[:1000] if description else None,
        "v": visibility,
        # allow_nan=False: 非正規 JSON を DB(IS JSON)まで運ばない(ルート側 422 と同じ契約)
        "c": json.dumps(config or {}, ensure_ascii=False, allow_nan=False),
    }
    if status is not None:
        cols += ", status"
        vals += ", :st"
        binds["st"] = status
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(f"INSERT INTO demos({cols}) VALUES ({vals})", **binds)
        conn.commit()
        cur.execute(f"SELECT {_COLS} FROM demos WHERE id = :id", id=demo_id)
        return _row_to_demo(cur.fetchone())


def get_demo(demo_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT {_COLS} FROM demos WHERE id = :id", id=demo_id)
        row = cur.fetchone()
        return _row_to_demo(row) if row else None


def list_demos(owner: str) -> list[dict[str, Any]]:
    """自分の所有のみ・updated_at DESC(specs/18 §2.1。公開横断一覧は SP4)。"""
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT {_COLS} FROM demos WHERE owner_sub = :o "
            "ORDER BY updated_at DESC FETCH FIRST 200 ROWS ONLY",
            o=owner,
        )
        return [_row_to_demo(r) for r in cur.fetchall()]


def update_demo(
    owner: str, demo_id: str, fields: dict[str, Any]
) -> dict[str, Any] | None:
    """name/description/visibility/config の部分更新。0行更新なら None(ルート側 404)。"""
    sets, binds = [], {"id": demo_id, "o": owner}
    for k, v in fields.items():
        if k not in _UPDATABLE:
            raise ValueError(f"non-updatable field: {k}")
        binds[k] = (
            json.dumps(v, ensure_ascii=False, allow_nan=False) if k == "config" else v
        )
        sets.append(f"{k} = :{k}")
    if not sets:
        raise ValueError("empty update (空 PATCH はルート側で現状返却)")
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE demos SET {', '.join(sets)}, updated_at = SYSTIMESTAMP "
            "WHERE id = :id AND owner_sub = :o",
            **binds,
        )
        if cur.rowcount == 0:
            return None
        conn.commit()
    return get_demo(demo_id)


def set_status(demo_id: str, from_status: str, to_status: str) -> bool:
    """specs/18 §1.2 の楽観遷移(WHERE status=:from で競合遷移を防ぐ)。SP2-01 ではルート未使用
    (provisioning/failed は SP3 予約・deleting は SP2-02 の DELETE が使う seam)。"""
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE demos SET status = :t, updated_at = SYSTIMESTAMP "
            "WHERE id = :id AND status = :f",
            t=to_status, id=demo_id, f=from_status,
        )
        conn.commit()
        return cur.rowcount > 0


def count_provisioning() -> int:
    """status='provisioning' の demo 総数(specs/19 §4.2 N3 の同時生成上限チェック)。
    グローバルロック区間(builder_generate)からのみ呼ぶ — 単独では TOCTOU を防げない。"""
    with connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM demos WHERE status = 'provisioning'")
        return int(cur.fetchone()[0])


def list_stale_provisioning(older_than_s: int) -> list[str]:
    """N1 ハードキャップを超えて provisioning のままの demo id(SP3-08 reconcile — ADR-0023 §4)。

    updated_at は set_status(→provisioning)で更新されるため「生成開始からの経過」を表す。
    遷移そのものは呼び出し側が set_status の楽観 UPDATE で行う(競合安全)。"""
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM demos WHERE status = 'provisioning' "
            "AND updated_at < SYSTIMESTAMP - NUMTODSINTERVAL(:s, 'SECOND')",
            s=older_than_s,
        )
        return [r[0] for r in cur.fetchall()]


def merge_config(demo_id: str, patch: dict[str, Any]) -> None:
    """サーバ管理 config キー(frontend/generation 等)を原子的にマージ(specs/19 §4.5 の公開)。

    JSON_MERGEPATCH で patch のキーだけ更新(値 null = キー削除。generation.error のクリアに使う)。
    owner PATCH の全置換とは別経路だが、preserve-merge(routes/demos §5.3)がサーバキーを
    温存するので通常は衝突しない。
    ponytail: read-then-replace の稀な lost-update は PoC 許容(§4.2 N7 の人間承認済み緩和)。
    """
    with connect() as conn:
        conn.cursor().execute(
            "UPDATE demos SET config = JSON_MERGEPATCH(config, :p RETURNING CLOB), "
            "updated_at = SYSTIMESTAMP WHERE id = :id",
            p=json.dumps(patch, ensure_ascii=False, allow_nan=False), id=demo_id,
        )
        conn.commit()


def delete_demo(owner: str, demo_id: str) -> bool:
    """行削除(所有者強制)。REST 公開は後始末込みで SP2-02(specs/18 §2.1)。"""
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM demos WHERE id = :id AND owner_sub = :o", id=demo_id, o=owner
        )
        conn.commit()
        return cur.rowcount > 0
