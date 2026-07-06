"""アプリ全体の DP Files 総数上限 = 予約 ledger(specs/18 §3.1)。

check-then-create 競合とクラッシュ回収を閉じる:
- 予約の直列化 = 専用 quota 行(RAG_FILE_QUOTA の単一行)の SELECT FOR UPDATE を予約 Tx の
  先頭で取り、件数検査 + pending 行 INSERT を同一 Tx で commit(超過は 422 に統一)。
- 外部作成後も pending のまま external_file_id だけを記録し、confirmed への更新は
  rag_files INSERT と同一 DB トランザクションのみ(confirmed = DB 登録済みの意味)。
- locator も write-ahead 保持(user 経路は demo_backend_targets を持たないため、
  region/project 変更後に旧 File を構成できるように — SP2-00 residual M001)。
- 表示名列は CHAR セマンティクス(rag_files は migration 024、ledger はここ — M002)。
- 起動時 reconcile: 期限切れ pending の exact 回収 / confirmed の回復マトリクス /
  ledger に無い実 File(未管理)は upload 経路 fail-closed(M006 — 勝手に消しも数えもしない)。

既定は無制限(rag_files_total_limit=None・挙動不変 = Public/main 互換)。記録は常に行う
(write-ahead は quota と独立に demo DELETE の枠解放・孤児回収の根拠になる)。
"""

import json
import logging
import uuid

from . import ddl_verify
from .db import connect
from .settings import get_settings

logger = logging.getLogger("jetuse.rag_ledger")

# 予約したまま外部作成が確認できない pending を回収対象とみなす経過時間
PENDING_STALE_S = 15 * 60


class QuotaExceededError(Exception):
    """アプリ全体の DP Files 上限超過(ルート側で 422 に正規化)。"""


class UnmanagedFilesError(Exception):
    """ledger に無い実 File を検出(fail-closed 503 — 人間対応。specs/18 §3.1)。"""


def _create_tolerant(cur, ddl: str, *ok_codes: str) -> None:
    """DDL を実行し、指定 ORA コード(同時作成 ORA-00955 等)は成功扱いにする(複数プロセス起動)。"""
    try:
        cur.execute(ddl)
    except Exception as e:  # noqa: BLE001
        if not any(code in str(e) for code in ok_codes):
            raise


def _require_pk(cur, table: str) -> None:
    cur.execute("SELECT COUNT(*) FROM user_constraints "
                "WHERE table_name = :t AND constraint_type = 'P'", t=table)
    if cur.fetchone()[0] != 1:
        raise ddl_verify.DdlShapeMismatch(f"{table}: 主キー制約が欠落/複数(人間対応)")


def _has_json_check(cur, table: str, column: str) -> bool:
    cur.execute("SELECT search_condition FROM user_constraints "
                "WHERE table_name = :t AND constraint_type = 'C'", t=table)
    needle = f"{column} IS JSON".upper()
    return any(c and needle in str(c).upper() for (c,) in cur.fetchall())


def _ensure_ledger(cur) -> None:
    """冪等 DDL + 辞書検証(specs/18 §3.1 完全 DDL)。各 DDL 直後停止 / 複数プロセス同時起動
    に耐える。upload gate 状態は quota 表に永続する(reconcile と uvicorn が別プロセスの
    ため in-process では効かない — codex review-5 B002)。"""
    _create_tolerant(cur,
        """CREATE TABLE rag_file_ledger (
             id VARCHAR2(36) PRIMARY KEY,
             owner_key VARCHAR2(255) NOT NULL,
             filename VARCHAR2(400 CHAR) NOT NULL,
             ext VARCHAR2(10) NOT NULL,
             external_file_id VARCHAR2(128),
             state VARCHAR2(10) NOT NULL
               CONSTRAINT ck_rag_ledger_state CHECK (state IN ('pending','confirmed')),
             locator CLOB NOT NULL CONSTRAINT ck_rag_ledger_loc CHECK (locator IS JSON),
             created_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
             updated_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
             CONSTRAINT uq_rag_ledger_ext UNIQUE (external_file_id))""",
        "ORA-00955")
    # 既存(または今作成した)表の形を列・PK・CHECK・UNIQUE・索引まで完全検証する(部分適用・
    # 同名異形で続行しない — codex review-5 M003)。形違いは DdlShapeMismatch で停止(人間対応)。
    _EXPECTED_COLS = {
        "ID": ("VARCHAR2", 36, "B", "N"),
        "OWNER_KEY": ("VARCHAR2", 255, "B", "N"),
        "FILENAME": ("VARCHAR2", 400, "C", "N"),
        "EXT": ("VARCHAR2", 10, "B", "N"),
        "EXTERNAL_FILE_ID": ("VARCHAR2", 128, "B", "Y"),
        "STATE": ("VARCHAR2", 10, "B", "N"),
        "LOCATOR": ("CLOB", None, None, "N"),
    }
    for col, (dt, cl, cu, nn) in _EXPECTED_COLS.items():
        if not ddl_verify.verify_column(cur, "RAG_FILE_LEDGER", col, data_type=dt,
                                        char_length=cl, char_used=cu, nullable=nn):
            raise ddl_verify.DdlShapeMismatch(f"RAG_FILE_LEDGER.{col} 欠落(人間対応)")
    for col in ("CREATED_AT", "UPDATED_AT"):  # TIMESTAMP NOT NULL(型文字列差を避け先頭一致)
        st = ddl_verify.column_state(cur, "RAG_FILE_LEDGER", col)
        if st is None or not st["data_type"].startswith("TIMESTAMP") or st["nullable"] != "N":
            raise ddl_verify.DdlShapeMismatch(f"RAG_FILE_LEDGER.{col}: TIMESTAMP NOT NULL 期待")
    _require_pk(cur, "RAG_FILE_LEDGER")
    if ddl_verify.check_constraint_state(
            cur, "RAG_FILE_LEDGER", "state IN ('pending','confirmed')") is None:
        raise ddl_verify.DdlShapeMismatch("RAG_FILE_LEDGER: state CHECK 不一致/欠落(人間対応)")
    if not _has_json_check(cur, "RAG_FILE_LEDGER", "locator"):
        raise ddl_verify.DdlShapeMismatch("RAG_FILE_LEDGER: locator IS JSON CHECK 欠落(人間対応)")
    cur.execute("SELECT COUNT(*) FROM user_constraints "
                "WHERE constraint_name = 'UQ_RAG_LEDGER_EXT' AND constraint_type = 'U'")
    if cur.fetchone()[0] != 1:
        raise ddl_verify.DdlShapeMismatch("RAG_FILE_LEDGER: external_file_id UNIQUE 欠落(人間対応)")
    for name, ddl in (
        ("IDX_RAG_LEDGER_STATE",
         "CREATE INDEX idx_rag_ledger_state ON rag_file_ledger(state, created_at)"),
        ("IDX_RAG_LEDGER_OWNER",
         "CREATE INDEX idx_rag_ledger_owner ON rag_file_ledger(owner_key)"),
    ):
        cur.execute("SELECT COUNT(*) FROM user_indexes WHERE index_name = :n", n=name)
        if cur.fetchone()[0] == 0:
            _create_tolerant(cur, ddl, "ORA-01408", "ORA-00955")

    # --- quota(単一アンカー行 + upload gate 状態の永続化) ---
    # gate 既定は 'N'(閉)= fail-closed(codex review-6 B001)。起動時 reconcile が未管理 File
    # ゼロを確認して初めて 'Y' に開く。reconcile 未実施/実行中/失敗の間は upload を通さない
    # (default 'Y' だと突合前に upload が素通りし総数上限・孤児検出の契約が崩れる)。
    _create_tolerant(cur,
        """CREATE TABLE rag_file_quota (
             id NUMBER PRIMARY KEY CONSTRAINT ck_rag_quota_one CHECK (id = 1),
             upload_gate_open CHAR(1) DEFAULT 'N' NOT NULL
               CONSTRAINT ck_rag_quota_gate CHECK (upload_gate_open IN ('Y','N')),
             gate_boot_id VARCHAR2(64))""",
        "ORA-00955")
    # 旧配備の quota 表に gate 列が無ければ追加(移行)。PK / id=1 CHECK / gate 列形を検証。
    if ddl_verify.column_state(cur, "RAG_FILE_QUOTA", "UPLOAD_GATE_OPEN") is None:
        _create_tolerant(cur,
            "ALTER TABLE rag_file_quota ADD (upload_gate_open CHAR(1) DEFAULT 'N' NOT NULL "
            "CONSTRAINT ck_rag_quota_gate CHECK (upload_gate_open IN ('Y','N')))",
            "ORA-01430")
    # gate_boot_id 列の移行(review-8 B001 — 起動世代でプロセス跨ぎの stale 'Y' を無効化)
    if ddl_verify.column_state(cur, "RAG_FILE_QUOTA", "GATE_BOOT_ID") is None:
        _create_tolerant(cur,
            "ALTER TABLE rag_file_quota ADD (gate_boot_id VARCHAR2(64))", "ORA-01430")
    if not ddl_verify.verify_column(cur, "RAG_FILE_QUOTA", "UPLOAD_GATE_OPEN",
                                    data_type="CHAR", char_length=1, char_used="B",
                                    nullable="N"):
        raise ddl_verify.DdlShapeMismatch("RAG_FILE_QUOTA.UPLOAD_GATE_OPEN 欠落(人間対応)")
    _require_pk(cur, "RAG_FILE_QUOTA")
    if ddl_verify.check_constraint_state(cur, "RAG_FILE_QUOTA", "id = 1") is None:
        raise ddl_verify.DdlShapeMismatch("RAG_FILE_QUOTA: id=1 CHECK 欠落(人間対応)")
    try:
        cur.execute("INSERT INTO rag_file_quota(id) VALUES (1)")
    except Exception as e:  # noqa: BLE001 — 初期行の作成競合(ORA-00001)は成功扱い
        if "ORA-00001" not in str(e):
            raise
    cur.connection.commit()


def current_locator() -> dict:
    """秘密値を除く完全 locator(specs/18 §3.1 — region / compartment / project / OS)。

    OpenSearch 有効時は endpoint も write-ahead 保存する。取り込み後に endpoint を無効化・変更
    しても、個別 DELETE が「取り込み時の endpoint」で旧 index を確実に消せるようにするため
    (現在設定に依存すると旧チャンクを消し損ねて検索可能なまま残る — B004 / §3.2)。
    """
    s = get_settings()
    loc = {
        "region": s.oci_region,
        "compartment": s.compartment_ocid,
        "project": s.project_ocid,
        "os_namespace": s.os_namespace,
        "bucket": s.rag_bucket,
    }
    if s.opensearch_endpoint:
        loc["opensearch_endpoint"] = s.opensearch_endpoint
    return loc


def _total_file_count(cur) -> int:
    """アプリ全体の実 File 総数 = ledger 行 + SP2-02 導入前から在る rag_files(ledger 未登録)。

    既存 File は ledger へ backfill せず(推測 locator で個別 delete が実体を孤児化するのを避ける
    — codex review-11 B001)、代わりに grandfather として数える。confirmed 行は rag_files と id を
    共有するので二重計上しない(rag_files 側は ledger 未登録のみ加算)。"""
    cur.execute("SELECT COUNT(*) FROM rag_file_ledger")
    n = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM user_tables WHERE table_name = 'RAG_FILES'")
    if cur.fetchone()[0]:
        cur.execute("SELECT COUNT(*) FROM rag_files rf WHERE rf.oci_file_id IS NOT NULL "
                    "AND NOT EXISTS (SELECT 1 FROM rag_file_ledger l WHERE l.id = rf.id)")
        n += cur.fetchone()[0]
    return n


def reserve(owner_key: str, filename: str, ext: str) -> str:
    """予約 Tx: quota 行 FOR UPDATE → 件数検査 → pending INSERT → commit。

    上限超過は QuotaExceededError(422)。上限 None(既定)は検査なし = 挙動不変。
    """
    limit = get_settings().rag_files_total_limit
    rid = str(uuid.uuid4())
    with connect() as conn:
        cur = conn.cursor()
        _ensure_ledger(cur)
        cur.execute("SELECT id FROM rag_file_quota WHERE id = 1 FOR UPDATE")
        if limit is not None:
            if _total_file_count(cur) >= limit:
                conn.rollback()
                raise QuotaExceededError(
                    f"RAG file quota exceeded (limit {limit})"
                )
        cur.execute(
            """INSERT INTO rag_file_ledger(id, owner_key, filename, ext, state, locator)
               VALUES (:id, :o, :f, :e, 'pending', :loc)""",
            id=rid, o=owner_key, f=filename[:400], e=ext.lstrip(".").lower()[:10],
            loc=json.dumps(current_locator(), ensure_ascii=False),
        )
        conn.commit()
    return rid


def set_external(reservation_id: str, external_file_id: str) -> None:
    """外部 File 作成後、pending のまま external_file_id だけを記録する。"""
    with connect() as conn:
        conn.cursor().execute(
            """UPDATE rag_file_ledger SET external_file_id = :x,
                 updated_at = SYSTIMESTAMP WHERE id = :id""",
            x=external_file_id, id=reservation_id,
        )
        conn.commit()


def confirm_in_tx(cur, reservation_id: str) -> None:
    """pending → confirmed。rag_files INSERT と同一 Tx で呼ぶこと(呼び出し側が commit)。"""
    cur.execute(
        """UPDATE rag_file_ledger SET state = 'confirmed', updated_at = SYSTIMESTAMP
           WHERE id = :id""",
        id=reservation_id,
    )


def release(reservation_id: str) -> None:
    """予約解放(行削除)。冪等。"""
    with connect() as conn:
        conn.cursor().execute(
            "DELETE FROM rag_file_ledger WHERE id = :id", id=reservation_id
        )
        conn.commit()


def count_for_owner(owner_key: str) -> int:
    """owner の予約 ledger 行数(pending+confirmed)。箱上限は rag_files 行でなくこれで測る
    (外部削除失敗で残した pending 行も数え、失敗と再 upload の反復で demo_max_rag_files を
    超えて外部 File を増やせないようにする — codex review-6 M002)。"""
    with connect() as conn:
        cur = conn.cursor()
        _ensure_ledger(cur)
        cur.execute("SELECT COUNT(*) FROM rag_file_ledger WHERE owner_key = :o", o=owner_key)
        return cur.fetchone()[0]


def rows_for_owner(owner_key: str) -> list[dict]:
    """owner の全行(pending/confirmed)。demo DELETE 手順 3c の解放列挙に使う。"""
    with connect() as conn:
        cur = conn.cursor()
        _ensure_ledger(cur)
        cur.execute(
            """SELECT id, filename, ext, external_file_id, state, locator
               FROM rag_file_ledger WHERE owner_key = :o""",
            o=owner_key,
        )
        return [
            {"id": r[0], "filename": r[1], "ext": r[2], "external_file_id": r[3],
             "state": r[4],
             "locator": r[5] if isinstance(r[5], dict) else json.loads(r[5] or "{}")}
            for r in cur.fetchall()
        ]


def rows_for_owner_by_id(reservation_id: str) -> dict | None:
    """予約 ID で 1 行を引く(個別 delete_file の locator 参照。無ければ None)。"""
    with connect() as conn:
        cur = conn.cursor()
        _ensure_ledger(cur)
        cur.execute(
            "SELECT id, ext, external_file_id, locator FROM rag_file_ledger WHERE id = :id",
            id=reservation_id,
        )
        r = cur.fetchone()
        if not r:
            return None
        return {"id": r[0], "ext": r[1], "external_file_id": r[2],
                "locator": r[3] if isinstance(r[3], dict) else json.loads(r[3] or "{}")}


def count() -> int:
    with connect() as conn:
        cur = conn.cursor()
        _ensure_ledger(cur)
        cur.execute("SELECT COUNT(*) FROM rag_file_ledger")
        return cur.fetchone()[0]


# --- 起動時 reconcile(specs/18 §3.1) ---

def _set_gate(cur, open_: bool) -> None:
    """upload gate 状態 + 開けた起動世代を quota 表に永続する(プロセス跨ぎで共有)。
    boot_id を一緒に書くことで「どの起動の reconcile が開けたか」を残す(review-8 B001)。"""
    cur.execute(
        "UPDATE rag_file_quota SET upload_gate_open = :v, gate_boot_id = :b WHERE id = 1",
        v="Y" if open_ else "N", b=(get_settings().app_boot_id or None))


def _gate_passes(gate_value, gate_boot_id, current_boot_id) -> bool:
    """gate が開く条件: 値が 'Y' かつ(起動世代追跡が有効なら)今回起動が開けたものであること。
    current_boot_id が空(単一プロセス/未設定)なら boot 照合はスキップ(値のみ判定 = 従来挙動)。
    有効時は前回起動の 'Y'(gate_boot_id 不一致)を通さない = プロセス境界の stale 'Y' を封じる。"""
    if gate_value != "Y":
        return False
    if not current_boot_id:
        return True
    return gate_boot_id == current_boot_id


def upload_gate() -> None:
    """DB 永続の fail-closed ゲート(未管理 File 検出 or reconcile 失敗で閉じる)。reconcile は
    uvicorn とは別プロセス(entrypoint.sh)で走るため状態を DB に置く。さらに起動世代 boot_id で
    「前回起動が残した 'Y'」を無効化し、今回起動の reconcile 完了まで fail-closed に保つ
    (review-8 B001 — bootstrap 完了前に uvicorn が受け付ける窓を封じる)。

    gate はアプリ全体 Files 総数上限(RAG_FILES_TOTAL_LIMIT)の不変条件を守るためのもの。上限
    未設定(既定 None = 無制限 = Public/main 互換)なら守るべき総数不変条件が無いので no-op にする。
    これが無いと、reconcile を回さない既定デプロイ(RUN_DB_BOOTSTRAP 未設定)で全 upload が恒久
    503 になる(codex review-10 B002)。上限を設定する Internal 配備だけ fail-closed を強制する。"""
    if get_settings().rag_files_total_limit is None:
        return
    with connect() as conn:
        cur = conn.cursor()
        _ensure_ledger(cur)
        cur.execute("SELECT upload_gate_open, gate_boot_id FROM rag_file_quota WHERE id = 1")
        row = cur.fetchone()
    gate_value = row[0] if row else None
    gate_boot = row[1] if row else None
    if not _gate_passes(gate_value, gate_boot, get_settings().app_boot_id):
        raise UnmanagedFilesError(
            "uploads disabled: startup reconcile has not confirmed a clean state "
            "(unmanaged OCI Files check pending or failed, or gate opened by a prior boot)"
        )


def close_upload_gate() -> None:
    """reconcile が完走できなかった場合の fail-closed(codex review-2 major — bootstrap が
    reconcile 例外を握りつぶした後に upload が素通りするのを防ぐ)。次回 reconcile 成功で開く。"""
    with connect() as conn:
        cur = conn.cursor()
        _ensure_ledger(cur)
        _set_gate(cur, False)
        conn.commit()


def _stale_pending(cur) -> list[dict]:
    cur.execute(
        """SELECT id, owner_key, ext, external_file_id, locator FROM rag_file_ledger
           WHERE state = 'pending'
             AND created_at < SYSTIMESTAMP - NUMTODSINTERVAL(:s, 'SECOND')""",
        s=PENDING_STALE_S,
    )
    return [
        {"id": r[0], "owner_key": r[1], "ext": r[2], "external_file_id": r[3],
         "locator": r[4] if isinstance(r[4], dict) else json.loads(r[4] or "{}")}
        for r in cur.fetchall()
    ]


def _loc_key(loc: dict | None) -> str:
    """locator の安定キー(dict 順不同でも同一 project を1つに畳む)。"""
    return json.dumps(loc or {}, sort_keys=True, ensure_ascii=False)


def reconcile(list_files_fn, delete_file_fn, delete_original_fn,
              confirmed_recover_fn) -> dict:
    """起動時 reconcile。I/O 依存は関数で注入する(テスト容易性 + rag.py との循環回避)。

    - list_files_fn(locator) -> list[{"id", "filename"}] … その locator の project の
      DP files.list を全件取得(ページネーション完走。locator=None は現在設定)
    - delete_file_fn(external_file_id, locator) … NotFound は成功扱い。locator で client 構成
    - delete_original_fn(owner_key, reservation_id, ext, locator) … 原本の exact 削除
    - confirmed_recover_fn(row, has_file: bool) … confirmed 行(locator 込み)の回復1件処理
    一覧の一時エラーは「不存在」と解釈せず例外のまま伝播(fail-closed — 後で再 reconcile)。
    行ごとの write-ahead locator を使い、region/project 変更後も旧 project の File を辿る
    (specs/18 §3.1)。set_external 前に停止した pending の File も、行 locator の project を
    file_key 名で照合して確実に削除する(旧 project に取り残さない — B002)。
    """
    summary = {"released_pending": 0, "confirmed_checked": 0, "unmanaged": 0}

    with connect() as conn:
        cur = conn.cursor()
        _ensure_ledger(cur)
        # 起動ごとに gate を先に閉じる(前回起動で残った 'Y' を今回の突合完了まで無効化する —
        # bootstrap は uvicorn と並行して走るため、閉じないと再起動直後・reconcile 進行中にも
        # 旧 'Y' で upload が素通りし、停止中に増えた未管理 File を見逃す。fail-closed / B001)。
        _set_gate(cur, False)
        conn.commit()
        stale = _stale_pending(cur)
        cur.execute("SELECT id, external_file_id, locator, state FROM rag_file_ledger")
        raw = cur.fetchall()
        # SP2-02 導入前から在る rag_files の外部 ID(ledger 未登録)。ledger へ backfill せず
        # 「管理下」として扱い、未管理誤判定=gate 恒久 503 を防ぐ(codex review-8 B003 / -11 B001)。
        cur.execute("SELECT COUNT(*) FROM user_tables WHERE table_name = 'RAG_FILES'")
        grandfathered_ext_ids: set = set()
        if cur.fetchone()[0]:
            cur.execute("SELECT oci_file_id FROM rag_files WHERE oci_file_id IS NOT NULL")
            grandfathered_ext_ids = {r[0] for r in cur.fetchall()}
        cur.execute(
            "SELECT id, owner_key, ext, external_file_id, locator FROM rag_file_ledger "
            "WHERE state = 'confirmed'"
        )
        confirmed = [
            {"id": r[0], "owner_key": r[1], "ext": r[2], "external_file_id": r[3],
             "locator": r[4] if isinstance(r[4], dict) else json.loads(r[4] or "{}")}
            for r in cur.fetchall()
        ]

    from .owner_keys import file_key  # 遅延 import(循環回避)

    ledger_rows = [
        {"id": r[0], "ext_id": r[1],
         "locator": r[2] if isinstance(r[2], dict) else json.loads(r[2] or "{}"),
         "state": r[3]}
        for r in raw
    ]
    known_ext_ids = {r["ext_id"] for r in ledger_rows if r["ext_id"]}
    # 外部 ID 未設定の「全」pending 行(files.create 済〜set_external 前の crash window)。
    # 15分未満の新鮮な pending も含める(stale だけに限ると再起動直後に exact file_key を
    # 誤って未管理判定し gate を人手対応まで閉じる — codex review-6 M001)。この rid の
    # file_key 名を持つ File だけを管理下とみなす(登録済み rid 名の別 ID File = 再試行の
    # 重複 → 未管理として検出 — M002)。
    pending_no_ext = {r["id"] for r in ledger_rows
                      if not r["ext_id"] and r["state"] == "pending"}

    # 現在設定 + 全行の distinct locator ごとに File 一覧を取得する(旧 project も走査)
    cur_loc = current_locator()
    locators: dict[str, dict] = {_loc_key(cur_loc): cur_loc}
    for r in ledger_rows:
        locators.setdefault(_loc_key(r["locator"]), r["locator"] or cur_loc)
    by_name_by_loc: dict[str, dict] = {}
    ids_by_loc: dict[str, set] = {}
    files_by_loc: dict[str, list] = {}
    for k, loc in locators.items():
        fs = list_files_fn(loc)  # 例外はそのまま(一時エラーを不存在と解釈しない)
        files_by_loc[k] = fs
        by_name_by_loc[k] = {f["filename"]: f["id"] for f in fs if f.get("filename")}
        ids_by_loc[k] = {f["id"] for f in fs}

    # 期限切れ pending: 行 locator の project から実 File(名前照合)/原本を探し削除→解放
    for row in stale:
        k = _loc_key(row.get("locator"))
        fkey = file_key(row["owner_key"], row["id"], row["ext"])
        ext_id = row["external_file_id"] or by_name_by_loc.get(k, {}).get(fkey)
        loc = row.get("locator") or None
        if ext_id:
            delete_file_fn(ext_id, loc)
        delete_original_fn(row["owner_key"], row["id"], row["ext"], loc)
        release(row["id"])
        summary["released_pending"] += 1

    # confirmed の回復マトリクス: 行 locator の project で File 実在を判定し locator 込みで委譲
    for row in confirmed:
        k = _loc_key(row.get("locator"))
        has_file = row["external_file_id"] in ids_by_loc.get(k, set())
        confirmed_recover_fn(row, has_file)
        summary["confirmed_checked"] += 1

    # 未管理 File = 現在設定の project にあり、ledger 登録も pending 名も grandfather(既存
    # rag_files)にも該当しない実 File → upload gate fail-closed(勝手に消さない)。既存 rag_files
    # の File は grandfathered_ext_ids で管理下扱い(backfill しないので誤判定=恒久 503 を防ぐ)。
    cur_files = files_by_loc[_loc_key(cur_loc)]
    unmanaged = [
        f for f in cur_files
        if f["id"] not in known_ext_ids
        and f["id"] not in grandfathered_ext_ids
        and not _is_pending_named(f.get("filename") or "", pending_no_ext)
    ]
    summary["unmanaged"] = len(unmanaged)
    with connect() as conn:  # gate 状態を DB に永続(配信プロセスから参照される — B002)
        cur = conn.cursor()
        _set_gate(cur, not unmanaged)
        conn.commit()
    if unmanaged:
        logger.error("reconcile: %d unmanaged OCI Files — upload gate closed",
                     len(unmanaged))
    return summary


def _is_pending_named(filename: str, pending_rids: set[str]) -> bool:
    """file_key 形式(<sha1>/<rid>.<ext>)の filename が、外部 ID 未設定の pending 行に対応するか。

    登録済み(external_file_id あり)の rid と同名でも別 ID の File は重複 = 未管理として扱う
    ため、ここでは pending(外部 ID 未設定)の rid だけを exemption 対象にする(M002)。
    """
    if "/" not in filename:
        return False
    base = filename.rsplit("/", 1)[1]
    rid = base.rsplit(".", 1)[0]
    return rid in pending_rids
