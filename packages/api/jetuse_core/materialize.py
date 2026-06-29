"""sample-app の dataset 定義を実テーブルへ自動マテリアライズする(BE-02)。

デモ起動 / scaffold 時に、`SampleAppDefinition` の datasets を
**実テーブルとして CREATE → seed 投入 → 読取ユーザ(adb_query_user)へ GRANT SELECT** する。
これにより NL2SQL(SBA-B 在庫照会等)が **新規デモ起動だけで動く**(手動マテリアライズ不要 /
ORA-00942 が出ない)。memory: jetuse-oke-deploy-feature-gaps ② の本質ギャップを塞ぐ。

設計上の不変条件:
  - **作成先 = 読取先 = `target_schema()`**(= `SAMPLE_DB_SCHEMA` か未設定なら adb_user)。
    作成は接続ユーザ自身のスキーマでしかできないため、`target_schema()≠adb_user` は
    `MaterializeConfigError` で**起動時に失敗**させる(作成先/読取先の不一致による ORA-00942 を
    未然に検出。別スキーマ展開は未対応)。sample_apps.py の専用 execute は CURRENT_SCHEMA をこの同じ
    `target_schema()` に固定する。
  - 読取ユーザ(adb_query_user)へ `GRANT SELECT`(実行は CREATE SESSION のみの最小権限)。
  - テーブル名は dataset 名(大文字)で、NL2SQL の許可テーブル `ds.name.upper()` と 1:1。物理列は
    dataset.fields から 1:1 生成(定義外の列を物理的に存在させない=列スコープ担保)。

安全性(破壊しない・混ぜない・壊れたら直す・直列):
  - **起動は非破壊**: 起動経路(recreate=False)では **データを持つ表を決して DROP しない**(顧客
    データの不可逆削除を人間ゲートなしに起こさない)。DROP するのは (a) 明示 `recreate=True`(管理者/
    E2E)、または (b) **空表**(行 0)の不完全(pending)/形不一致の作り直し のみ。列幅は固定(4000)で
    fingerprint は seed 非依存なので、サンプル変更だけで再構築は起きない。
  - **seed 状態の追跡**: レジストリに seeded を記録。空のまま作られた表(seeded=False)へ後から
    seeded=True 起動が来たら、**空のときに限り** seed を注入する(False→True / True→False の両順序で
    破壊せず期待どおり)。
  - **所有権**: レジストリに owner を記録。管理外の同名物理表(他者の表)や **別 owner**
    の同名表は触らず `MaterializeConflictError`(別アプリのデータ混在/誤再利用/無断 DROP を防ぐ)。
  - **直列化**: 同一 schema.table の materialize を `DBMS_LOCK`(セッションロック)で直列化(同時起動の
    部分作成・二重 DROP を防ぐ)。**DBMS_LOCK 不可は fail-closed**(縮退しない。プロビジョニング
    が EXECUTE を付与する: bootstrap.py / ops/setup-dev-schema.py)。
  - **回復性**: status を pending→ready で遷移。DDL は暗黙コミットでトランザクションにならないため、
    途中失敗(CREATE 後 seed/GRANT 前に落ちた等)は pending のまま残り、次回ロック下で安全に再構築。

CSV アップロード経路(datasets.py)は改変しない(BE-02 非ゴール)。専用外部スキーマで運用するアプリ
(SBA-C / JETUSE_SBA04)は auto-materialize の対象外(`materialize_app` がスキップ)。
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import json
import logging
import re
from typing import Any

from .db import connect
from .plugins.sample_app import Dataset, DatasetField, SampleAppDefinition
from .settings import get_settings

logger = logging.getLogger("jetuse.materialize")

#: Oracle 識別子として安全なスキーマ/ユーザ名のみ受け付ける(素で SQL に差し込むため)。
_SCHEMA_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]{0,127}$")

#: string 列幅(BYTE)。VARCHAR2 の物理上限(標準 MAX_STRING_SIZE)に合わせ 4000 固定。
_STR_MAX = 4000

#: 所有権レジストリ表(接続スキーマに置く)。自分が作った表のみ管理する。
_REGISTRY = "JETUSE_MATERIALIZED_DATASETS"

#: 物理表に直接焼く検証可能な所有マーカー(table comment)。レジストリ(別表)が壊れても/外部表が
#: 同名で割り込んでも、**この表自身に焼かれた owner** でしか DROP を許さない(fail-closed)。
#: 形式: 'JETUSE_MAT:<owner>'。レジストリ claim だけを信用して他者の表を消すのを防ぐ(F-002)。
_MARKER_PREFIX = "JETUSE_MAT"

#: owner 未指定時の安定 sentinel。Oracle は空文字を NULL に格納するため、空 owner をそのまま使うと
#: owner_id が NULL 化し所有比較(= 物理マーカー)が壊れる。空は必ずこの非空値へ正規化する(F-004)。
_DEFAULT_OWNER = "_unspecified"

#: owner_id 列(VARCHAR2(200))の上限(BYTE)。超過は予測可能な ValueError で弾く(F-004)。
_OWNER_MAX = 200

#: マテリアライズ仕様バージョン。DDL 生成規則(列型マップ・採寸)を変えたら上げる → fingerprint が
#: 変わり既存表が安全に再構築される。
_SPEC_VERSION = "2"

#: DBMS_LOCK.REQUEST のロックモード(6 = X_MODE = 排他)とタイムアウト秒。
_LOCK_X_MODE = 6
_LOCK_TIMEOUT_S = 60


class MaterializeConflictError(RuntimeError):
    """管理外/別 owner の同名物理表が在る。reuse/recreate せず fail-closed。"""


class MaterializeConfigError(RuntimeError):
    """設定不整合(作成先≠読取先、DBMS_LOCK 不可 等)。起動時に明示的に失敗させる。"""


def _norm_owner(owner: str) -> str:
    """owner を安定した非空識別子へ正規化する(空→sentinel、長すぎは拒否)。F-004。"""
    o = (owner or "").strip() or _DEFAULT_OWNER
    if len(o.encode("utf-8")) > _OWNER_MAX:
        raise ValueError(f"owner が長すぎます(<= {_OWNER_MAX} BYTE): {o[:40]}…")
    return o


def target_schema() -> str:
    """dataset の **作成先 かつ 読取の CURRENT_SCHEMA 固定先**(単一の出所)。

    `SAMPLE_DB_SCHEMA` 設定時はそれを、未設定なら接続ユーザ(adb_user)自身のスキーマ。
    """
    s = get_settings()
    return s.sample_db_schema or s.adb_user


def oracle_type(field_type: str, *, str_len: int = _STR_MAX) -> str:
    """dataset の宣言型(FieldType)を Oracle のカラム型へマップする。

    string→VARCHAR2(str_len BYTE) / text→CLOB / number→NUMBER / boolean→NUMBER(1) /
    date→DATE / datetime→TIMESTAMP。未知型は保守的に VARCHAR2 へフォールバック。string は BYTE
    セマンティクスを明示し NLS 依存をなくす(GROUP BY 不可の CLOB とは分ける)。
    """
    return {
        "string": f"VARCHAR2({str_len} BYTE)",
        "text": "CLOB",
        "number": "NUMBER",
        "boolean": "NUMBER(1)",
        "date": "DATE",
        "datetime": "TIMESTAMP",
    }.get(field_type, f"VARCHAR2({str_len} BYTE)")


def _column_type(field: DatasetField) -> str:
    """物理列型。string は **常に VARCHAR2(4000 BYTE)** にする(seed 値で幅を狭めない)。

    seed のサンプル長で狭く採寸すると、後続の replace_later 顧客データ差替えで ORA-12899 になる
    (F-005)。また seed 値が変わるだけで列幅=fingerprint が変わり既存表の再構築を誘発する(F-001)。
    幅を一定にしてこの両方を避ける。VARCHAR2(4000) は使った分だけ消費するので過大割当てにならない。
    4000 BYTE 超の seed 値は VARCHAR2 上限超なので呼び出し側が弾く(`_assert_string_widths`)。
    """
    return oracle_type(field.type)


def _fingerprint(ds: Dataset) -> str:
    """dataset の物理形の指紋。fields(名前/固定列型)＋ 仕様版のみで構成する。

    seed の **内容/長さ/件数**・seeded は含めない。列幅は固定(4000)で seed 値が変わっても
    fingerprint は不変 → 起動は非破壊で、サンプル変更だけで既存(顧客)データを消さない(F-001)。
    形/型/DDL 規則(仕様版)の変化だけを「互換性なし」とする。
    """
    shape = {
        "spec": _SPEC_VERSION,
        "cols": [(f.name.upper(), _column_type(f)) for f in ds.fields],
    }
    return hashlib.sha256(
        json.dumps(shape, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _assert_string_widths(ds: Dataset) -> None:
    """seed の string 値が VARCHAR2 上限(4000 BYTE)を超えないことを保証する(超過は ORA-12899)。"""
    for f in ds.fields:
        if f.type != "string":
            continue
        for i, row in enumerate(ds.seed):
            v = row.get(f.name)
            if isinstance(v, str) and len(v.encode("utf-8")) > _STR_MAX:
                raise ValueError(
                    f"dataset '{ds.name}' seed[{i}] フィールド '{f.name}' が VARCHAR2 上限"
                    f"({_STR_MAX} BYTE)超。長文は type=text(CLOB)にしてください"
                )


def _assert_unique_physical_columns(ds: Dataset) -> None:
    """物理化(大文字化)後の列名が衝突しないことを保証する(例: foo と FOO は同じ物理列)。"""
    upper = [f.name.upper() for f in ds.fields]
    dup = sorted({n for n in upper if upper.count(n) > 1})
    if dup:
        raise ValueError(
            f"dataset '{ds.name}': 物理化(大文字)後に列名が衝突します(大小だけ異なる列): {dup}"
        )


def _coerce(field_type: str, value: Any) -> Any:
    """seed の JSON 値を bind 用の Python 値へ変換する(型は定義検証済み = JSON 整合保証)。

    date/datetime は ISO 文字列なので date/datetime へ。boolean は 0/1。空文字は NULL(date 列へ
    "" を入れて ORA-01858 になるのを防ぐ)。
    """
    if value is None or value == "":
        return None
    if field_type == "boolean":
        return 1 if value else 0
    if field_type == "date":
        return _dt.date.fromisoformat(value) if isinstance(value, str) else value
    if field_type == "datetime":
        return _dt.datetime.fromisoformat(value) if isinstance(value, str) else value
    return value


def _col_ident(field: DatasetField) -> str:
    return f'"{field.name.upper()}"'


def _table_ident(schema: str, ds: Dataset) -> str:
    return f'{schema}."{ds.name.upper()}"'


def _ensure_registry(cur) -> None:
    """所有権レジストリ表を冪等に用意する(接続スキーマ。-955=既存は無視)。"""
    cur.execute(
        f"""
        BEGIN
          EXECUTE IMMEDIATE 'CREATE TABLE {_REGISTRY} (
            schema_name VARCHAR2(128), table_name VARCHAR2(128),
            owner_id VARCHAR2(200), fingerprint VARCHAR2(64),
            seeded NUMBER(1) DEFAULT 0, status VARCHAR2(16),
            updated_at TIMESTAMP DEFAULT SYSTIMESTAMP,
            CONSTRAINT {_REGISTRY}_PK PRIMARY KEY (schema_name, table_name))';
        EXCEPTION WHEN OTHERS THEN IF SQLCODE != -955 THEN RAISE; END IF;
        END;
        """
    )


@contextlib.contextmanager
def _table_lock(conn, schema: str, table: str):
    """schema.table 単位の排他ロックで materialize を直列化する(DBMS_LOCK セッションロック)。

    `release_on_commit=FALSE` なので間の DDL 暗黙コミットでも保持される。**DBMS_LOCK 不可は
    fail-closed**(`MaterializeConfigError`。縮退しない。プロビジョニングが EXECUTE を付与)。
    """
    cur = conn.cursor()
    handle = None
    try:
        h = cur.var(str)
        cur.callproc(
            "DBMS_LOCK.ALLOCATE_UNIQUE", [f"JETUSE_MAT_{schema}.{table}"[:128], h]
        )
        handle = h.getvalue()
        status = cur.callfunc(
            "DBMS_LOCK.REQUEST", int, [handle, _LOCK_X_MODE, _LOCK_TIMEOUT_S, False]
        )
        if status not in (0, 4):  # 0=success, 4=already own
            raise MaterializeConfigError(
                f"DBMS_LOCK.REQUEST status={status} for {schema}.{table}"
            )
    except MaterializeConfigError:
        raise
    except Exception as e:  # noqa: BLE001 — ロック不可は fail-closed
        raise MaterializeConfigError(
            "DBMS_LOCK が利用できません(materialize の直列化に必須)。app ユーザへ "
            f"GRANT EXECUTE ON DBMS_LOCK を付与してください: {str(e).splitlines()[0][:120]}"
        ) from e
    try:
        yield
    finally:
        if handle is not None:
            with contextlib.suppress(Exception):
                cur.callfunc("DBMS_LOCK.RELEASE", int, [handle])


def _registry_get(cur, schema: str, table: str) -> dict[str, Any] | None:
    cur.execute(
        f"SELECT status, fingerprint, owner_id, seeded FROM {_REGISTRY} "
        "WHERE schema_name = :s AND table_name = :t",
        s=schema.upper(), t=table.upper(),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {"status": row[0], "fingerprint": row[1], "owner": row[2],
            "seeded": bool(row[3])}


def _registry_set(
    cur, schema: str, table: str, *, owner: str, fp: str, seeded: bool, status: str
) -> None:
    cur.execute(
        f"""
        MERGE INTO {_REGISTRY} r
        USING (SELECT :s AS schema_name, :t AS table_name FROM dual) k
        ON (r.schema_name = k.schema_name AND r.table_name = k.table_name)
        WHEN MATCHED THEN UPDATE SET owner_id = :o, fingerprint = :fp,
                                     seeded = :sd, status = :st, updated_at = SYSTIMESTAMP
        WHEN NOT MATCHED THEN INSERT (schema_name, table_name, owner_id, fingerprint,
                                      seeded, status)
             VALUES (:s, :t, :o, :fp, :sd, :st)
        """,
        s=schema.upper(), t=table.upper(), o=owner, fp=fp,
        sd=(1 if seeded else 0), st=status,
    )


def _registry_delete(cur, schema: str, table: str) -> None:
    """レジストリの claim を取り消す(外部表割り込み等で所有を確立できなかったとき)。"""
    cur.execute(
        f"DELETE FROM {_REGISTRY} WHERE schema_name = :s AND table_name = :t",
        s=schema.upper(), t=table.upper(),
    )


def _set_marker(cur, table_id: str, owner: str) -> None:
    """物理表に所有マーカー(table comment)を焼く。CREATE 直後に呼ぶ(その表が確かに自分の作)。"""
    esc = f"{_MARKER_PREFIX}:{owner}".replace("'", "''")
    cur.execute(f"COMMENT ON TABLE {table_id} IS '{esc}'")


def _marker_owner(cur, schema: str, table: str) -> str | None:
    """物理表に焼かれた所有マーカーの owner を返す(マーカー無し/別形式は None)。"""
    cur.execute(
        "SELECT comments FROM all_tab_comments WHERE owner = :o AND table_name = :t",
        o=schema.upper(), t=table.upper(),
    )
    row = cur.fetchone()
    if not row or not row[0]:
        return None
    comment = row[0]
    prefix = _MARKER_PREFIX + ":"
    return comment[len(prefix):] if comment.startswith(prefix) else None


def _assert_owned(cur, schema: str, table: str, owner: str) -> None:
    """既存物理表に触れる(reuse/GRANT/seed/DROP)前に **物理表自身のマーカー** で所有を検証する。

    レジストリ(別表)が ready でも、**物理表が外部置換**されていればマーカーは欠落/別 owner になる。
    その表は「自分が作った表」と検証できないので **一切触らない**(`MaterializeConflictError`)。
    GRANT で他者表を読取ユーザへ晒す/他者データを DROP する事故を全経路で防ぐ(F-002)。
    """
    marker = _marker_owner(cur, schema, table)
    if marker != owner:
        raise MaterializeConflictError(
            f"{schema}.{table} の所有マーカー(owner={marker!r})が現在の owner '{owner}' と不一致。"
            "所有を物理表で検証できないため触りません(fail-closed)"
        )


def _table_exists(cur, schema: str, table: str) -> bool:
    cur.execute(
        "SELECT 1 FROM all_tables WHERE owner = :o AND table_name = :t",
        o=schema.upper(), t=table.upper(),
    )
    return cur.fetchone() is not None


def _row_count(cur, table_id: str) -> int:
    cur.execute(f"SELECT COUNT(*) FROM {table_id}")
    return int(cur.fetchone()[0])


def _seed_rows(cur, table_id: str, ds: Dataset) -> int:
    """ds.seed を table へ投入し行数を返す(列順固定の positional bind)。"""
    if not ds.seed:
        return 0
    cols = ", ".join(_col_ident(f) for f in ds.fields)
    placeholders = ", ".join(f":{i + 1}" for i in range(len(ds.fields)))
    payload = [[_coerce(f.type, row.get(f.name)) for f in ds.fields] for row in ds.seed]
    cur.executemany(f"INSERT INTO {table_id} ({cols}) VALUES ({placeholders})", payload)
    return len(payload)


def _build(cur, schema: str, query_user: str, ds: Dataset, *, owner: str, seeded: bool) -> int:
    """CREATE → 所有マーカー焼付 → (seeded なら) seed 投入 → GRANT SELECT。投入行数を返す。

    CREATE 直後に `_set_marker` で物理表へ owner を焼く(後で DROP 可否はこのマーカーで検証する)。
    直列化(ロック)下で呼ぶ。
    """
    table_id = _table_ident(schema, ds)
    coldefs = ", ".join(f"{_col_ident(f)} {_column_type(f)}" for f in ds.fields)
    cur.execute(f"CREATE TABLE {table_id} ({coldefs})")
    _set_marker(cur, table_id, owner)
    rows = _seed_rows(cur, table_id, ds) if seeded else 0
    cur.execute(f"GRANT SELECT ON {table_id} TO {query_user}")
    return rows


def _materialize_dataset(
    conn, schema: str, query_user: str, ds: Dataset, *,
    owner: str, recreate: bool, seeded: bool,
) -> dict[str, Any]:
    """1 dataset を実テーブルへ展開(直列化＋レジストリで安全に)。返り {name, table, rows, action}。

    **データを持つ表は起動経路(recreate=False)では決して DROP しない**(顧客データの不可逆削除を
    人間ゲートなしに起こさない)。DROP するのは (a) 明示 `recreate=True`(管理者/E2E)、または
    (b) **空表**(行 0)の不完全/形不一致の作り直し のみ。空の未 seed 表へ後から seeded=True が来たら
    (空のときに限り)seed を注入する。
    """
    _assert_string_widths(ds)
    _assert_unique_physical_columns(ds)
    table = ds.name.upper()
    table_id = _table_ident(schema, ds)
    fp = _fingerprint(ds)

    def _rebuild(*, drop: bool) -> dict[str, Any]:
        # DROP は物理表のマーカーで所有を検証してから(claim だけでは他者表を消さない。F-002)。
        if drop:
            _assert_owned(cur, schema, table, owner)
        _registry_set(cur, schema, table, owner=owner, fp=fp, seeded=seeded, status="pending")
        conn.commit()
        if drop:
            cur.execute(f"DROP TABLE {table_id} PURGE")
        try:
            rows = _build(cur, schema, query_user, ds, owner=owner, seeded=seeded)
        except Exception as e:  # noqa: BLE001
            # CREATE が ORA-00955(名前既使用)= 我々の claim 中に外部表が割り込んだ。所有を確立
            # できないので claim を取り消し fail-closed(その表を後で誤って DROP しない。F-002)。
            if "ORA-00955" in str(e):
                _registry_delete(cur, schema, table)
                conn.commit()
                raise MaterializeConflictError(
                    f"{schema}.{table} の作成中に同名表が外部で出現しました(ORA-00955)。"
                    "所有を確立できないため中止します(reuse/DROP しません)"
                ) from e
            raise
        _registry_set(cur, schema, table, owner=owner, fp=fp, seeded=seeded, status="ready")
        conn.commit()
        return {"name": ds.name, "table": table, "rows": rows,
                "action": "recreated" if drop else "created"}

    with _table_lock(conn, schema, table):
        cur = conn.cursor()
        reg = _registry_get(cur, schema, table)
        phys = _table_exists(cur, schema, table)

        # 管理外の物理表(他者の表)は触らない(fail-closed)。
        if reg is None and phys:
            raise MaterializeConflictError(
                f"{schema}.{table} は materialize 管理外の既存表です(reuse/recreate しません)"
            )
        # 別 owner(別アプリ)の同名表は混ぜない(fail-closed)。
        if reg is not None and reg["owner"] != owner:
            raise MaterializeConflictError(
                f"{schema}.{table} は別アプリ('{reg['owner']}')所有です(現在 '{owner}')"
            )

        if not phys:
            return _rebuild(drop=False)  # 新規 / レジストリだけ残存 → 作成

        # phys=True(管理下)。触れる前に必ず
        # **物理表のマーカーで所有を検証**(レジストリ ready でも外部置換なら触らない。
        # GRANT で他者表を晒す/他者データを消す事故を全経路で防ぐ。F-002)。
        _assert_owned(cur, schema, table, owner)

        if recreate:
            return _rebuild(drop=True)  # 明示の破壊的再作成(唯一データを消し得る経路)

        ready_ok = reg is not None and reg["status"] == "ready" and reg["fingerprint"] == fp
        if ready_ok:
            # 空の未 seed 表へ seeded=True → 空のときに限り seed 注入(非破壊・両順序対応)。
            if seeded and not reg["seeded"] and _row_count(cur, table_id) == 0:
                rows = _seed_rows(cur, table_id, ds)
                cur.execute(f"GRANT SELECT ON {table_id} TO {query_user}")
                _registry_set(cur, schema, table, owner=owner, fp=fp, seeded=True,
                              status="ready")
                conn.commit()
                return {"name": ds.name, "table": table, "rows": rows, "action": "seeded"}
            cur.execute(f"GRANT SELECT ON {table_id} TO {query_user}")  # 権限の保険
            return {"name": ds.name, "table": table, "rows": 0, "action": "reused"}

        # ready_ok でない = pending(作りかけ) または fingerprint 不一致(形ドリフト)。
        empty = _row_count(cur, table_id) == 0
        if empty:
            # 空表は所有検証済み・データ損失ゼロなので安全に作り直す(pending 回復 / 形変更)。
            return _rebuild(drop=True)

        fp_mismatch = reg is not None and reg["fingerprint"] != fp
        if fp_mismatch:
            # **非空 × 形不一致** = schema ドリフト。温存したまま明示 conflict で起動を失敗させる
            # (silent reuse で古い形のまま ready に戻さない。recreate=True か migration が必要。F-2)
            raise MaterializeConflictError(
                f"{schema}.{table} は定義の形(fingerprint)と不一致でデータがあります。"
                "起動では作り直しません。recreate=True かスキーマ移行を行ってください"
            )

        # 非空 × 形一致 × pending = 我々の作りかけにデータが入っている → finalize(ready)して reuse。
        logger.info("%s.%s は pending だがデータあり・形一致 → ready に確定し reuse", schema, table)
        cur.execute(f"GRANT SELECT ON {table_id} TO {query_user}")
        _registry_set(cur, schema, table, owner=owner, fp=fp,
                      seeded=reg["seeded"], status="ready")
        conn.commit()
        return {"name": ds.name, "table": table, "rows": 0, "action": "reused"}


def materialize_definition(
    definition: SampleAppDefinition,
    *,
    owner: str = "",
    schema: str | None = None,
    query_user: str | None = None,
    recreate: bool = False,
    seeded: bool = True,
) -> dict[str, Any]:
    """sample-app 定義の datasets を実テーブルへ展開し、読取ユーザへ SELECT 付与する。

    作成先 = 読取先 = `target_schema()`(= 接続ユーザ)。`target_schema()≠adb_user` は直接呼び出しでは
    `MaterializeConfigError`(プログラム誤用への fail-closed。launch 経路 `materialize_app` はこの
    構成を legacy 事前プロビジョニング扱いでスキップし起動を壊さない=F-003)。起動は非破壊・冪等
    (既存 ready は reuse)。`owner` は所有アプリ識別子(空→sentinel・別 owner の同名表は混ぜない)。
    `seeded=False` は表だけ作って seed しない。

    返り {schema, query_user, owner, recreate, seeded, datasets:[{name, table, rows, action}]}。
    """
    settings = get_settings()
    owner = _norm_owner(owner)  # 空→sentinel(Oracle 空文字→NULL を避ける)・長さ検証(F-004)
    schema = (schema or target_schema()).strip()
    if not _SCHEMA_RE.match(schema):
        raise ValueError(f"不正なスキーマ識別子: {schema!r}")
    conn_user = (settings.adb_user or "").strip()
    if schema.upper() != conn_user.upper():
        raise MaterializeConfigError(
            f"作成先 {schema} が接続ユーザ {conn_user} と異なります。SAMPLE_DB_SCHEMA は未設定か "
            f"adb_user と同一にしてください(別スキーマ展開は未対応)"
        )
    query_user = (query_user or settings.adb_query_user).strip()
    if not _SCHEMA_RE.match(query_user):
        raise ValueError(f"不正な読取ユーザ識別子: {query_user!r}")

    results: list[dict[str, Any]] = []
    if definition.datasets:
        with connect() as conn:
            cur = conn.cursor()
            _ensure_registry(cur)
            conn.commit()
            for ds in definition.datasets:
                results.append(
                    _materialize_dataset(
                        conn, schema, query_user, ds,
                        owner=owner, recreate=recreate, seeded=seeded,
                    )
                )
    logger.info(
        "materialized %d dataset(s) into %s (owner=%s, grant→%s, recreate=%s, seeded=%s)",
        len(results), schema, owner, query_user, recreate, seeded,
    )
    return {
        "schema": schema,
        "query_user": query_user,
        "owner": owner,
        "recreate": recreate,
        "seeded": seeded,
        "datasets": results,
    }


def materialize_app(
    instance_id: str, *, recreate: bool = False, seeded: bool = True
) -> dict[str, Any] | None:
    """コア同梱 sample-app(instance_id)の datasets を実テーブルへ展開する。

    `resolve_app` で検証済み定義を引き、`materialize_definition` に委譲する(owner=instance_id)。
    未知 instance は None。**専用外部スキーマで運用するアプリ(SBA-C / nl2sql_schema≠target_schema)
    は auto-materialize の対象外**(誤った adb_user 表を作らない)。`seeded` は合成のシード方針
    (`SeedPlan.seeded`)を反映(デモ起動経路 hearing.launch_demo が渡す)。

    **後方互換(F-003)**: `SAMPLE_DB_SCHEMA` が接続ユーザ(adb_user)と別スキーマを指す構成は、データを
    事前プロビジョニングした **legacy スキーマ運用** とみなし auto をスキップする(起動を
    `MaterializeConfigError` で失敗させない。読取は従来どおりその `SAMPLE_DB_SCHEMA` を使う)。
    """
    # 遅延 import(registry → materialize の循環を避け、settings/DB 依存も呼び出し時に閉じる)。
    from .plugins.sample_app_registry import resolve_app

    resolved = resolve_app(instance_id)
    if resolved is None:
        return None
    ns = resolved.nl2sql_schema
    if ns:
        # **専用 nl2sql_schema を宣言するアプリ(SBA-C / JETUSE_SBA04)は自前プロビジョニング**で運用
        # するため auto-materialize の対象外(target_schema と一致/不一致に関わらずスキップ。誤った
        # adb_user 表を作らず、既存の専用スキーマ運用へ干渉しない)。BE-02 の auto 対象は
        # nl2sql_schema=None(データを target_schema=adb_user に置く)アプリ(SBA-B 等)に限る。
        logger.info(
            "skip auto-materialize for %s (dedicated nl2sql_schema=%s)", instance_id, ns
        )
        return {"schema": ns, "datasets": [], "skipped": "dedicated_schema"}
    # SAMPLE_DB_SCHEMA が接続ユーザと別 = 事前プロビジョニング(legacy)運用 → auto はスキップ。
    # launch を壊さず、その既存スキーマを読取先として使う(materialize_definition の config error は
    # 直接呼び出し=明示的なプログラム誤用に対する fail-closed として温存する)。
    ts = target_schema()
    conn_user = (get_settings().adb_user or "").strip()
    if ts.upper() != conn_user.upper():
        logger.info(
            "skip auto-materialize for %s (pre-provisioned SAMPLE_DB_SCHEMA=%s ≠ adb_user=%s)",
            instance_id, ts, conn_user,
        )
        return {"schema": ts, "datasets": [], "skipped": "pre_provisioned_schema"}
    if not resolved.definition.datasets:
        return {"schema": ts, "datasets": []}
    return materialize_definition(
        resolved.definition, owner=instance_id, recreate=recreate, seeded=seeded
    )
