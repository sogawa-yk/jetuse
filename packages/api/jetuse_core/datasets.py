"""構造化データ(CSV)アップロード→DBチャット対象化(ENH-01)。

オブジェクトストレージ/DBMS_CLOUDを使わずアプリ直結ロード:
CSV解析 → JETUSE_APP に CREATE TABLE → executemany投入 → JETUSE_QUERY に SELECT付与 →
ユーザー専用 Select AI プロファイル(object_list=本人のデータセット表)を再構築。

NL2SQLは target=datasets のとき本人プロファイルで生成し、JETUSE_QUERY(読取専用)で実行する。
SPIKE-ENH01で実機確認済み(create+load+grant→Select AI showsql→read-only exec)。
"""

import csv
import hashlib
import io
import json
import logging
import re
import time
import uuid
from typing import Any

from . import ddl_verify, nl2sql, vpd
from .db import connect
from .demo_lease import DemoLease, require_lease_for
from .owner_keys import is_demo_namespace, owner_key_gate
from .settings import get_settings

logger = logging.getLogger("jetuse.datasets")

MAX_ROWS = 5000
MAX_COLS = 60
# creating のまま残った登録行を残骸とみなす経過時間(specs/18 §3.2 手順 2)
CREATING_STALE_S = 15 * 60


class DatasetDeleteError(Exception):
    """DROP 先行削除の失敗(登録簿行を残して 503 — 再試行で収束)。"""
GEN_MODEL = "gemini-2.5-flash"  # サンプルデータ生成(CSV書式と列名指定の遵守が安定)
MAX_GEN_ROWS = 200
# Select AIがデータを認識するまで(プロファイル再構築直後)のウォームアップ上限(feedback 20260620 #2)
WARMUP_TIMEOUT_S = 45
WARMUP_INTERVAL_S = 4

# 作成済みデータセットプロファイルのモデルをプロセス内に記録(モデル変更時に作り直すため)
_ds_profile_models: dict[str, str] = {}


def _schema() -> str:
    """アプリスキーマ(=接続ユーザー)。開発者ごとに分ける場合は ADB_USER で切替わる。"""
    return get_settings().adb_user


def _query_user() -> str:
    """読取専用ユーザー。adb_user と対で開発者ごとに分ける。"""
    return get_settings().adb_query_user


def _owner_tag(owner: str) -> str:
    """demo namespace は完全 sha1(40hex)。8hex 衝突は共有プロファイルの object_list を通じた
    メタデータ漏えい + 削除波及になる(specs/18 §3.2 手順 2)。既存 user 資産の 8hex は
    変えない(main 互換 — user 側の衝突リスクは main バックポート課題の residual)。"""
    h = hashlib.sha1(owner.encode()).hexdigest()
    return (h if is_demo_namespace(owner) else h[:8]).upper()


def profile_name(owner: str) -> str:
    return f"JETUSE_DS_{_owner_tag(owner)}"


def _ident(name: str, fallback: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_]", "_", (name or "").strip())
    s = re.sub(r"_+", "_", s).strip("_").upper()
    if not s or not s[0].isalpha():
        s = f"C_{s}".strip("_")
    return s[:30] or fallback


def _is_num(v: str) -> bool:
    try:
        float(v.replace(",", ""))
        return True
    except (ValueError, AttributeError):
        return False


def _ensure_meta(cur) -> None:
    cur.execute(
        """
        BEGIN
          EXECUTE IMMEDIATE 'CREATE TABLE JETUSE_DATASETS (
            ds_id VARCHAR2(40) PRIMARY KEY, owner_sub VARCHAR2(255),
            table_name VARCHAR2(130), display_name VARCHAR2(200),
            columns_json CLOB, row_count NUMBER, created TIMESTAMP DEFAULT SYSTIMESTAMP)';
        EXCEPTION WHEN OTHERS THEN IF SQLCODE != -955 THEN RAISE; END IF;
        END;
        """
    )
    _ensure_state_column(cur)


_STATE_CHECK = "state IN ('creating','ready')"


def _ensure_state_column(cur) -> None:
    """state 列の導入(specs/18 §3.2 手順 2)。冪等性は ORA コード無視でなく辞書検証で担保:
    各ステップの前に現状を検証し、不足分だけを適用。形違いは停止して人間対応。

    (1) NULL 許容で追加 → (2) 既存行 backfill(旧 writer は CREATE 成功後登録 = ready が正)
    → (3) DEFAULT 'ready' → (4) NOT NULL + 正規名付き CHECK。
    """
    has = ddl_verify.verify_column(cur, "JETUSE_DATASETS", "STATE",
                                   data_type="VARCHAR2", char_length=10, char_used="B")
    if not has:
        cur.execute("ALTER TABLE JETUSE_DATASETS ADD (state VARCHAR2(10))")
    cur.execute("UPDATE JETUSE_DATASETS SET state = 'ready' WHERE state IS NULL")
    cur.connection.commit()
    st = ddl_verify.column_state(cur, "JETUSE_DATASETS", "STATE")
    if (st.get("data_default") or "").strip("' ") != "ready":
        cur.execute("ALTER TABLE JETUSE_DATASETS MODIFY (state DEFAULT 'ready')")
    existing = ddl_verify.check_constraint_state(cur, "JETUSE_DATASETS", _STATE_CHECK)
    if existing is None:
        cur.execute(
            "ALTER TABLE JETUSE_DATASETS ADD CONSTRAINT ck_jetuse_datasets_state "
            f"CHECK ({_STATE_CHECK})"
        )
    elif existing != "CK_JETUSE_DATASETS_STATE":
        raise ddl_verify.DdlShapeMismatch(
            f"JETUSE_DATASETS: state CHECK が別名 {existing} で存在(期待 "
            "CK_JETUSE_DATASETS_STATE)。停止(人間対応が必要)"
        )
    st = ddl_verify.column_state(cur, "JETUSE_DATASETS", "STATE")
    if st["nullable"] == "Y":
        cur.execute("ALTER TABLE JETUSE_DATASETS MODIFY (state NOT NULL)")


def _rebuild_profile(owner: str, cur, model: str | None = None) -> list[str]:
    """本人のデータセット表だけを object_list にした Select AI プロファイルを作り直す。

    返り値は object_list に含めたテーブル名(ウォームアップ用)。model はモデル選択(#3)。
    """
    model = nl2sql.resolve_select_ai_model(model)
    # ready のみ参照: 途中クラッシュの creating 幽霊行を object_list に混ぜて
    # dbchat を壊さない(specs/18 §3.2 手順 2)
    cur.execute(
        "SELECT table_name FROM JETUSE_DATASETS WHERE owner_sub = :o "
        "AND state = 'ready'",
        o=owner,
    )
    tables = [r[0] for r in cur.fetchall()]
    prof = profile_name(owner)
    if not tables:
        try:
            cur.execute("BEGIN DBMS_CLOUD_AI.DROP_PROFILE(:p); END;", p=prof)
        except Exception:  # noqa: BLE001
            pass
        _ds_profile_models.pop(prof, None)
        return []
    nl2sql.create_profile(
        cur, prof, model, [{"owner": _schema(), "name": t} for t in tables]
    )
    _ds_profile_models[prof] = model
    return tables


def _warmup_profile(prof: str, tables: list[str]) -> bool:
    """プロファイル再構築直後、Select AIが当該テーブルを認識するまで待つ(feedback 20260620 #2)。

    showsql を試行し、生成SQLにいずれかのテーブル名が現れたら準備完了とみなす。
    上限まで認識されなければ False(UIで「認識中」を案内する)。
    """
    if not tables:
        return True
    upper = [t.upper() for t in tables]
    probe = f"{tables[0]} のデータを5件見せて"
    deadline = time.monotonic() + WARMUP_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            sql = nl2sql.generate_sql_select_ai(probe, profile_name=prof)
            if any(t in sql.upper() for t in upper):
                return True
        except Exception:  # noqa: BLE001
            logger.debug("warmup probe failed (retrying)", exc_info=True)
        time.sleep(WARMUP_INTERVAL_S)
    logger.warning("select ai profile %s not ready within %ss", prof, WARMUP_TIMEOUT_S)
    return False


def ensure_profile(owner: str, model: str | None = None,
                   lease: DemoLease | None = None) -> str:
    """本人データセット用プロファイルを指定モデルで用意し名前を返す(NL2SQL生成の直前に呼ぶ)。

    プロセス内キャッシュのモデルと異なる場合のみ作り直し+ウォームアップする
    (モデル選択 feedback #3 / 準備待ち #2)。demo namespace はリース保持が前提(specs/18 §3.2.1)。
    """
    vpd.integrity_gate()
    owner_key_gate()
    require_lease_for(owner, lease)
    model = nl2sql.resolve_select_ai_model(model)
    prof = profile_name(owner)
    if _ds_profile_models.get(prof) == model:
        return prof
    with connect() as conn:
        cur = conn.cursor()
        _ensure_meta(cur)
        reconcile_creating(owner, cur=cur)  # creating 残骸は次のデータセット操作時に回収
        tables = _rebuild_profile(owner, cur, model)
        conn.commit()
    if tables:
        _warmup_profile(prof, tables)
    return prof


def create_dataset(
    owner: str, display_name: str, data: bytes,
    model: str | None = None, warmup: bool = True,
    lease: DemoLease | None = None,
) -> dict[str, Any]:
    vpd.integrity_gate()
    owner_key_gate()
    require_lease_for(owner, lease)  # demo namespace はリース保持が前提(specs/18 §3.2.1)
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if any((c or "").strip() for c in r)]
    if len(rows) < 2:
        raise ValueError("ヘッダ行とデータ行が必要です")
    header, body = rows[0], rows[1:][:MAX_ROWS]
    if len(header) > MAX_COLS:
        raise ValueError(f"列数が多すぎます(上限{MAX_COLS})")

    cols, seen = [], set()
    for i, h in enumerate(header):
        c = _ident(h, f"COL{i + 1}")
        while c in seen:
            c = f"{c[:27]}_{i}"
        seen.add(c)
        cols.append(c)
    ncol = len(cols)
    body = [r[:ncol] + [""] * (ncol - len(r)) for r in body]

    types = []
    for j in range(ncol):
        vals = [r[j] for r in body]
        nonempty = [v for v in vals if (v or "").strip()]
        if nonempty and all(_is_num(v) for v in nonempty):
            types.append("NUMBER")
        else:
            # Oracleの既定はBYTEセマンティクスのため、日本語(UTF-8で1文字3バイト)が
            # 文字数基準だと ORA-12899 になる。バイト長で見積もり余裕を持たせる
            maxbytes = max((len((v or "").encode("utf-8")) for v in vals), default=1)
            types.append(f"VARCHAR2({min(max(maxbytes * 2, 32), 4000)})")

    ds_id = str(uuid.uuid4())
    table = f"JETUSE_DS_{_owner_tag(owner)}_{ds_id[:8].upper()}"
    coldefs = ", ".join(f'"{c}" {t}' for c, t in zip(cols, types, strict=True))
    placeholders = ", ".join(f":{i + 1}" for i in range(ncol))

    def conv(v: str, t: str):
        v = (v or "").strip()
        if t == "NUMBER":
            return float(v.replace(",", "")) if v else None
        return v or None

    payload = [[conv(r[j], types[j]) for j in range(ncol)] for r in body]

    with connect() as conn:
        cur = conn.cursor()
        _ensure_meta(cur)
        reconcile_creating(owner, cur=cur)  # creating 残骸の回収(specs/18 §3.2 手順 2)
        if is_demo_namespace(owner):
            # 箱あたり上限(specs/18 §3.1 — 同期削除の所要時間を構成的に有界化)
            cur.execute(
                "SELECT COUNT(*) FROM JETUSE_DATASETS WHERE owner_sub = :o", o=owner
            )
            if cur.fetchone()[0] >= get_settings().demo_max_datasets:
                raise ValueError(
                    f"データセット数の上限({get_settings().demo_max_datasets})に達しています"
                )
        schema = _schema()
        # registry-first(specs/18 §3.2 手順 2): 登録簿 INSERT(state='creating')を commit して
        # から CREATE TABLE → VPD → GRANT → ready。未登録の表を構造的に排除し、
        # 登録簿を削除根拠にできるようにする(短縮 tag の接頭辞走査を削除根拠にしない)。
        cur.execute(
            """INSERT INTO JETUSE_DATASETS(ds_id, owner_sub, table_name, display_name,
                 columns_json, row_count, state)
               VALUES (:i,:o,:t,:d,:c,:r,'creating')""",
            i=ds_id, o=owner, t=table, d=(display_name or table)[:200],
            c=json.dumps(cols), r=len(payload),
        )
        conn.commit()
        created_table = False
        try:
            cur.execute(f'CREATE TABLE {schema}.{table} ({coldefs})')
            created_table = True  # これ以降の失敗のみ「自分が作った表」= DROP してよい
            if payload:
                cur.executemany(
                    f'INSERT INTO {schema}.{table} VALUES ({placeholders})', payload
                )
            # ADD_POLICY 成功 → GRANT の順(specs/18 §4.3 — 無保護表を晒さない)。VPD 無効の
            # Public/従来環境では DBMS_RLS/context/policy 未配備なので付与しない(無条件実行だと
            # 最初の dataset 作成が必ず失敗する — codex review-10 B004。分離なし = 単一利用者前提)。
            if get_settings().vpd_enabled:
                vpd.apply_policy(cur, table)
            cur.execute(f'GRANT SELECT ON {schema}.{table} TO {_query_user()}')
        except Exception:
            # 失敗収束。CREATE TABLE 自体の失敗(ORA-00955 名前衝突を含む)では table が他データ
            # セットの実表かもしれず絶対に DROP しない(既存データ損失防止 — review-12 B001)。
            # 自分が作った表だけ DROP し、他要因で DROP 失敗なら 'creating' 行を残し reconcile に
            # 委ねる(孤児実表の放置防止)。ORA-00942(表なし)は消えている=行削除して良い。
            drop_ok = True
            if created_table:
                try:
                    cur.execute(f'DROP TABLE {schema}.{table} PURGE')
                except Exception as de:  # noqa: BLE001
                    drop_ok = "ORA-00942" in str(de)
            if drop_ok:
                try:
                    cur.execute("DELETE FROM JETUSE_DATASETS WHERE ds_id = :i", i=ds_id)
                    conn.commit()
                except Exception:  # noqa: BLE001
                    pass
            raise
        cur.execute(
            "UPDATE JETUSE_DATASETS SET state = 'ready' WHERE ds_id = :i", i=ds_id
        )
        tables = _rebuild_profile(owner, cur, model)
        conn.commit()
    # データ投入直後はSelect AIが表を認識するまで時間がかかる(feedback 20260620 #2)。
    # warmup=Falseは複数投入(サンプル一括)時に最後だけまとめてウォームアップするため。
    ready = _warmup_profile(profile_name(owner), tables) if warmup else True
    logger.info("dataset created: %s (%d rows, %d cols, ready=%s)",
                table, len(payload), ncol, ready)
    return {"id": ds_id, "table_name": table, "display_name": display_name,
            "columns": cols, "row_count": len(payload), "ready": ready}


def _strip_fences(text: str) -> str:
    """LLM出力からコードフェンス(```csv 等)や前置きを剥がしてCSV本体を取り出す。"""
    t = (text or "").strip()
    m = re.match(r"^```[A-Za-z]*\s*\n(.*?)\n```\s*$", t, re.S)
    if m:
        return m.group(1).strip()
    # 念のため行頭フェンスだけ落とす保険
    lines = [ln for ln in t.splitlines() if not ln.strip().startswith("```")]
    return "\n".join(lines).strip()


def generate_dataset(
    owner: str, description: str, display_name: str | None = None, rows: int = 30,
    model: str | None = None,
) -> dict[str, Any]:
    """AIでサンプルデータ(CSV)を生成→データセット化(feedback 20260618-3)。

    どういう内容のデータかを説明文で渡すだけで、LLMが列設計とサンプル行を作る。
    生成CSVは既存の create_dataset でそのまま投入する(検証・型推定を共用)。
    """
    from .chat import complete_once

    desc = (description or "").strip()
    if not desc:
        raise ValueError("生成するデータの説明を入力してください")
    n = max(1, min(int(rows or 30), MAX_GEN_ROWS))
    prompt = (
        "あなたはサンプルデータ生成アシスタントです。以下の説明に沿った"
        "ダミーのサンプルデータをCSV形式だけで生成してください。\n"
        "ルール(厳守):\n"
        "- 1行目はヘッダ(列名)。列名は必ず半角英小文字スネークケース(ASCII)にする。"
        "日本語や全角文字を列名に使ってはならない。"
        "例: order_date,product_name,quantity,unit_price,amount\n"
        "- 2行目以降がデータ。値は日本語でよい。\n"
        f"- データ行はちょうど{n}行。\n"
        "- 値は現実的でばらつきのある内容にする(同じ値の単純な繰り返しを避ける)。\n"
        "- 各行の列数はヘッダと必ず一致させる。\n"
        "- セルにカンマや改行を含めない(必要なら言い換える)。\n"
        "- CSV以外の説明文・前置き・コードブロック記号(```)は一切出力しない。\n\n"
        f"説明: {desc}"
    )
    raw = complete_once(GEN_MODEL, [{"role": "user", "content": prompt}], max_chars=60000)
    csv_text = _strip_fences(raw)
    if not csv_text.strip():
        raise ValueError("サンプルデータの生成に失敗しました(空応答)")
    name = (display_name or "").strip() or desc[:40]
    return create_dataset(owner, name, csv_text.encode("utf-8"), model=model)


def seed_samples(owner: str, model: str | None = None) -> dict[str, Any]:
    """既定のサンプルデータセットを本人スキーマへ一括投入(feedback 20260620 #12)。

    既に同名のデータセットがある場合はスキップする。ウォームアップは最後に一度だけ行う。
    """
    from .sample_data import SAMPLE_DATASETS

    existing = {d["display_name"] for d in list_datasets(owner)}
    created: list[dict[str, Any]] = []
    for display_name, csv_text in SAMPLE_DATASETS:
        if display_name in existing:
            continue
        created.append(
            create_dataset(owner, display_name, csv_text.encode("utf-8"),
                           model=model, warmup=False)
        )
    ready = True
    if created:
        ready = _warmup_profile(
            profile_name(owner), [c["table_name"] for c in created]
        )
        for c in created:
            c["ready"] = ready
    return {"datasets": created, "ready": ready,
            "skipped": len(SAMPLE_DATASETS) - len(created)}


def list_datasets(owner: str) -> list[dict[str, Any]]:
    """一覧は state='ready' のみ(creating 幽霊行を表示しない — specs/18 §3.2 手順 2)。"""
    vpd.integrity_gate()  # 不整合時は dbchat/datasets 経路を 503 で停止(specs/18 §4.3)
    with connect() as conn:
        cur = conn.cursor()
        _ensure_meta(cur)
        cur.execute(
            """SELECT ds_id, table_name, display_name, columns_json, row_count
               FROM JETUSE_DATASETS WHERE owner_sub = :o AND state = 'ready'
               ORDER BY created DESC""",
            o=owner,
        )
        return [
            {"id": r[0], "table_name": r[1], "display_name": r[2],
             "columns": json.loads(r[3] or "[]"), "row_count": int(r[4] or 0)}
            for r in cur.fetchall()
        ]


def preview(owner: str, ds_id: str, limit: int = 20) -> dict[str, Any]:
    """データセット表の中身(サンプル行)を返す。ds_idは本人所有を検証(ENH-02拡張)。

    VPD 導入後は owner コンテキスト付きで実行する(登録簿で本人検証済みだが
    同関数経由に統一 — specs/18 §4.3 呼び出し元の移行契約)。
    """
    from . import nl2sql

    vpd.integrity_gate()
    ds = next((d for d in list_datasets(owner) if d["id"] == ds_id), None)
    if not ds:
        raise ValueError("データセットが見つかりません")
    n = max(1, min(int(limit), 50))
    return nl2sql.execute_readonly(
        f'SELECT * FROM {_schema()}."{ds["table_name"]}" FETCH FIRST {n} ROWS ONLY',
        owner_key=owner,
    )


def _drop_table_strict(cur, table: str) -> None:
    """DROP TABLE PURGE。ORA-00942(不存在)のみ成功扱い、他は DatasetDeleteError。"""
    try:
        cur.execute(f'DROP TABLE {_schema()}.{table} PURGE')
    except Exception as e:
        if "ORA-00942" in str(e):
            return
        raise DatasetDeleteError(f"drop {table} failed: {str(e)[:200]}") from e


def delete_dataset(owner: str, ds_id: str, lease: DemoLease | None = None) -> bool:
    """DROP 先行(specs/18 §3.2 手順 2 の前提改修): ORA-00942 のみ成功扱い、
    他の失敗は登録簿行を残して DatasetDeleteError(503) — 再試行で収束。"""
    vpd.integrity_gate()
    owner_key_gate()
    require_lease_for(owner, lease)
    with connect() as conn:
        cur = conn.cursor()
        _ensure_meta(cur)
        cur.execute(
            "SELECT table_name FROM JETUSE_DATASETS WHERE ds_id = :i AND owner_sub = :o",
            i=ds_id, o=owner,
        )
        row = cur.fetchone()
        if not row:
            return False
        table = row[0]
        _drop_table_strict(cur, table)  # DROP 成功(or 不存在)を確認してから行を消す
        cur.execute("DELETE FROM JETUSE_DATASETS WHERE ds_id = :i AND owner_sub = :o",
                    i=ds_id, o=owner)
        _rebuild_profile(owner, cur)
        conn.commit()
    return True


# --- 後始末・回収の公開関数(demo_cleanup / vpd.verify_integrity が使う) ---


def reconcile_creating(owner: str | None = None, *, cur=None,
                       min_age_s: int = CREATING_STALE_S) -> int:
    """creating のまま一定時間を過ぎた登録行を回収する(表があれば DROP → 行 DELETE。冪等)。

    min_age_s=0 は demo DELETE / 起動時 reconcile 用(進行中 create はリース/単一インスタンス
    前提で並行しない)。回収件数を返す。
    """
    if cur is None:
        with connect() as conn:
            return reconcile_creating(owner, cur=conn.cursor(), min_age_s=min_age_s)
    if not ddl_verify.table_exists(cur, "JETUSE_DATASETS"):
        return 0  # fresh schema: 登録簿がまだ無い(初回 dataset 作成前) — 回収対象なし
    _ensure_meta(cur)  # 旧登録簿(STATE 列なし)を先に移行(SELECT state の ORA-00904 回避 — B001)
    sql = ("SELECT ds_id, table_name FROM JETUSE_DATASETS WHERE state = 'creating' "
           "AND created < SYSTIMESTAMP - NUMTODSINTERVAL(:age, 'SECOND')")
    binds: dict[str, Any] = {"age": min_age_s}
    if owner is not None:
        sql += " AND owner_sub = :o"
        binds["o"] = owner
    cur.execute(sql, **binds)
    rows = cur.fetchall()
    for ds_id, table in rows:
        _drop_table_strict(cur, table)
        cur.execute("DELETE FROM JETUSE_DATASETS WHERE ds_id = :i", i=ds_id)
    if rows:
        cur.connection.commit()
        logger.info("reconciled %d creating dataset leftovers", len(rows))
    return len(rows)


def registry_rows(owner: str) -> list[dict[str, Any]]:
    """exact owner 一致の全登録行(state 不問)。demo DELETE 手順 2 の削除根拠。"""
    with connect() as conn:
        cur = conn.cursor()
        _ensure_meta(cur)
        cur.execute(
            "SELECT ds_id, table_name, state FROM JETUSE_DATASETS WHERE owner_sub = :o",
            o=owner,
        )
        return [{"id": r[0], "table_name": r[1], "state": r[2]} for r in cur.fetchall()]


def delete_owner(owner: str) -> None:
    """owner の DB 箱の後始末(specs/18 §3.2 手順 2): 登録簿の exact 一致行を列挙して
    DROP TABLE PURGE(ORA-00942 成功扱い)→ 行削除 → プロファイル DROP(不存在は無視)。

    呼び出し側(demo_cleanup)がリースを保持している前提。失敗は DatasetDeleteError(503)。
    """
    with connect() as conn:
        cur = conn.cursor()
        _ensure_meta(cur)
        cur.execute(
            "SELECT ds_id, table_name FROM JETUSE_DATASETS WHERE owner_sub = :o", o=owner
        )
        for ds_id, table in cur.fetchall():
            _drop_table_strict(cur, table)
            cur.execute("DELETE FROM JETUSE_DATASETS WHERE ds_id = :i", i=ds_id)
        prof = profile_name(owner)
        # 不存在は事前確認で無視(ORA コードの一律無視で権限/タイムアウト/DBMS_CLOUD_AI 障害を
        # 隠さない — codex review-2 blocker)。存在する profile の DROP 失敗は 503 で行を保持。
        cur.execute(
            "SELECT COUNT(*) FROM user_cloud_ai_profiles WHERE profile_name = :p", p=prof
        )
        if cur.fetchone()[0]:
            try:
                cur.execute("BEGIN DBMS_CLOUD_AI.DROP_PROFILE(:p); END;", p=prof)
            except Exception as e:
                raise DatasetDeleteError(
                    f"drop profile {prof} failed: {str(e)[:200]}"
                ) from e
        _ds_profile_models.pop(prof, None)
        conn.commit()
