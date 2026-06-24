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

from . import nl2sql
from .db import connect
from .settings import get_settings

logger = logging.getLogger("jetuse.datasets")

MAX_ROWS = 5000
MAX_COLS = 60
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
    return hashlib.sha1(owner.encode()).hexdigest()[:8].upper()


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


def _rebuild_profile(owner: str, cur, model: str | None = None) -> list[str]:
    """本人のデータセット表だけを object_list にした Select AI プロファイルを作り直す。

    返り値は object_list に含めたテーブル名(ウォームアップ用)。model はモデル選択(#3)。
    """
    model = nl2sql.resolve_select_ai_model(model)
    cur.execute(
        "SELECT table_name FROM JETUSE_DATASETS WHERE owner_sub = :o", o=owner
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


def ensure_profile(owner: str, model: str | None = None) -> str:
    """本人データセット用プロファイルを指定モデルで用意し名前を返す(NL2SQL生成の直前に呼ぶ)。

    プロセス内キャッシュのモデルと異なる場合のみ作り直し+ウォームアップする
    (モデル選択 feedback #3 / 準備待ち #2)。
    """
    model = nl2sql.resolve_select_ai_model(model)
    prof = profile_name(owner)
    if _ds_profile_models.get(prof) == model:
        return prof
    with connect() as conn:
        cur = conn.cursor()
        _ensure_meta(cur)
        tables = _rebuild_profile(owner, cur, model)
        conn.commit()
    if tables:
        _warmup_profile(prof, tables)
    return prof


def create_dataset(
    owner: str, display_name: str, data: bytes,
    model: str | None = None, warmup: bool = True,
) -> dict[str, Any]:
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
        schema = _schema()
        cur.execute(f'CREATE TABLE {schema}.{table} ({coldefs})')
        if payload:
            cur.executemany(f'INSERT INTO {schema}.{table} VALUES ({placeholders})', payload)
        cur.execute(f'GRANT SELECT ON {schema}.{table} TO {_query_user()}')
        cur.execute(
            """INSERT INTO JETUSE_DATASETS(ds_id, owner_sub, table_name, display_name,
                 columns_json, row_count) VALUES (:i,:o,:t,:d,:c,:r)""",
            i=ds_id, o=owner, t=table, d=(display_name or table)[:200],
            c=json.dumps(cols), r=len(payload),
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
    with connect() as conn:
        cur = conn.cursor()
        _ensure_meta(cur)
        cur.execute(
            """SELECT ds_id, table_name, display_name, columns_json, row_count
               FROM JETUSE_DATASETS WHERE owner_sub = :o ORDER BY created DESC""",
            o=owner,
        )
        return [
            {"id": r[0], "table_name": r[1], "display_name": r[2],
             "columns": json.loads(r[3] or "[]"), "row_count": int(r[4] or 0)}
            for r in cur.fetchall()
        ]


def preview(owner: str, ds_id: str, limit: int = 20) -> dict[str, Any]:
    """データセット表の中身(サンプル行)を返す。ds_idは本人所有を検証(ENH-02拡張)。"""
    from . import nl2sql

    ds = next((d for d in list_datasets(owner) if d["id"] == ds_id), None)
    if not ds:
        raise ValueError("データセットが見つかりません")
    n = max(1, min(int(limit), 50))
    return nl2sql.execute_readonly(
        f'SELECT * FROM {_schema()}."{ds["table_name"]}" FETCH FIRST {n} ROWS ONLY'
    )


def delete_dataset(owner: str, ds_id: str) -> bool:
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
        cur.execute("DELETE FROM JETUSE_DATASETS WHERE ds_id = :i AND owner_sub = :o",
                    i=ds_id, o=owner)
        try:
            cur.execute(f'DROP TABLE {_schema()}.{table} PURGE')
        except Exception:  # noqa: BLE001
            logger.exception("drop dataset table failed (ignored): %s", table)
        _rebuild_profile(owner, cur)
        conn.commit()
    return True
