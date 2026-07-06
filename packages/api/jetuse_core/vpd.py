"""VPD 基盤(specs/18 §4.3 層1 — SP2-02 所有)。datasets 表の行レベル境界を DB 側で張る。

- アプリケーションコンテキスト(<ADB_USER>_CTX)+ setter パッケージ(JETUSE_VPD_CTX)+
  ポリシー関数(JETUSE_VPD_POLICY)+ 各 JETUSE_DS_* 表への DBMS_RLS ポリシー(JETUSE_DS_POLICY)。
- 照合は exact 一致のみ: ポリシー関数は JETUSE_DATASETS 登録簿(表名 → exact owner_sub)を引き、
  コンテキストの OWNER_KEY と一致時のみ全行。対応行なし・コンテキスト未設定は必ず 0 行
  (fail-closed)。動的 SQL・関数経由でもポリシーは適用される(実機確認済み —
  runs/2026-07-06T1113_SP2-02/e2e/feasibility.md)。
- 所有スキーマ(アプリ内部経路)には適用しない: ポリシー関数が SESSION_USER = 所有スキーマの
  ときだけ全行を返す(specs/18 §4.3「JETUSE_APP には適用しない」)。
- 初回セットアップ(CREATE ANY CONTEXT / EXECUTE ON DBMS_RLS・DBMS_LOCK の付与、既存表への
  一括ポリシー付与)は人間承認のうえ実行(APPROVAL-REQUEST.md)。通常起動の bootstrap は
  「検証 + 承認済み定義の冪等再適用」に限定(実行時のアプリ資格情報は昇格しない)。
- 起動時の完全性検証は fail-closed: まず registry の creating 残骸を reconcile してから、
  「query user へ SELECT 付与された全オブジェクト」「全 JETUSE_DS_* 表」を実在から列挙し、
  登録簿の exact な 1 行と VPD ポリシーが揃うことを検証。不明・不整合は dbchat/datasets
  経路を 503 で停止(他機能は起動継続)。
"""

import logging

from . import ddl_verify
from .db import connect
from .settings import get_settings

logger = logging.getLogger("jetuse.vpd")

POLICY_NAME = "JETUSE_DS_POLICY"
CTX_PACKAGE = "JETUSE_VPD_CTX"
POLICY_FUNCTION = "JETUSE_VPD_POLICY"
LOCK_PACKAGE = "JETUSE_LOCK"

_integrity_ok = False


class DatasetsSecurityError(Exception):
    """VPD 完全性が未検証/不整合(dbchat・datasets 経路は 503 で停止)。"""


def context_name() -> str:
    """コンテキスト名はスキーマごとに一意(同一 ADB 内の並行スキーマと衝突させない)。"""
    return f"{get_settings().adb_user.upper()}_CTX"


def _schema() -> str:
    return get_settings().adb_user.upper()


# --- 承認済み定義(冪等再適用可能な CREATE OR REPLACE 群) ---


def lock_definitions() -> list[str]:
    """排他リース cover package(specs/18 §3.2.1 — REQUEST/RELEASE の最小機能のみ晒す)。

    VPD の有無に依らず demo 操作の直列化に必要(Internal/Public 双方)。DBMS_LOCK EXECUTE の
    付与が前提(人間ゲート)。付与前は package body が不正コンパイルし、lease acquire が
    LeaseUnavailableError=503 で fail-closed(review-13 B001)。
    """
    return [
        f"""CREATE OR REPLACE PACKAGE {LOCK_PACKAGE} AS
  FUNCTION request(p_id IN INTEGER, p_timeout IN INTEGER) RETURN INTEGER;
  FUNCTION release(p_id IN INTEGER) RETURN INTEGER;
END {LOCK_PACKAGE};""",
        f"""CREATE OR REPLACE PACKAGE BODY {LOCK_PACKAGE} AS
  FUNCTION request(p_id IN INTEGER, p_timeout IN INTEGER) RETURN INTEGER IS
  BEGIN
    RETURN DBMS_LOCK.REQUEST(id => p_id, lockmode => DBMS_LOCK.X_MODE,
                             timeout => p_timeout, release_on_commit => FALSE);
  END request;
  FUNCTION release(p_id IN INTEGER) RETURN INTEGER IS
  BEGIN
    RETURN DBMS_LOCK.RELEASE(id => p_id);
  END release;
END {LOCK_PACKAGE};""",
    ]


def vpd_definitions() -> list[str]:
    """VPD 固有定義(context setter / context / policy function)。Internal 専用。"""
    schema, ctx = _schema(), context_name()
    return [
        # コンテキスト setter(CREATE CONTEXT の USING に紐づく信頼パッケージ)
        f"""CREATE OR REPLACE PACKAGE {CTX_PACKAGE} AS
  PROCEDURE set_owner(p_owner IN VARCHAR2);
  PROCEDURE clear_owner;
END {CTX_PACKAGE};""",
        f"""CREATE OR REPLACE PACKAGE BODY {CTX_PACKAGE} AS
  PROCEDURE set_owner(p_owner IN VARCHAR2) IS
  BEGIN
    DBMS_SESSION.SET_CONTEXT('{ctx}', 'OWNER_KEY', p_owner);
  END set_owner;
  PROCEDURE clear_owner IS
  BEGIN
    DBMS_SESSION.CLEAR_CONTEXT('{ctx}', NULL, 'OWNER_KEY');
  END clear_owner;
END {CTX_PACKAGE};""",
        f"CREATE OR REPLACE CONTEXT {ctx} USING {schema}.{CTX_PACKAGE}",
        # ポリシー関数: 登録簿 exact 一致のみ全行。所有スキーマ(アプリ内部)は適用外。
        f"""CREATE OR REPLACE FUNCTION {POLICY_FUNCTION}(
  obj_schema IN VARCHAR2, obj_name IN VARCHAR2) RETURN VARCHAR2 IS
  v_owner VARCHAR2(255);
BEGIN
  IF SYS_CONTEXT('USERENV', 'SESSION_USER') = '{schema}' THEN
    RETURN '1=1';
  END IF;
  SELECT owner_sub INTO v_owner
    FROM {schema}.JETUSE_DATASETS WHERE table_name = obj_name;
  IF v_owner = SYS_CONTEXT('{ctx}', 'OWNER_KEY') THEN
    RETURN '1=1';
  END IF;
  RETURN '1=0';
EXCEPTION
  WHEN NO_DATA_FOUND THEN RETURN '1=0';
  WHEN TOO_MANY_ROWS THEN RETURN '1=0';
END;""",
    ]


def approved_definitions() -> list[str]:
    """人間承認の対象定義(APPROVAL-REQUEST.md に添付する正本)= 排他リース + VPD。"""
    return lock_definitions() + vpd_definitions()


def reapply_definitions() -> None:
    """承認済み定義の冪等再適用。JETUSE_LOCK cover package は VPD の有無に依らず作る(排他リースは
    Internal/Public 双方の demo 操作で必要 — codex review-13 B001。VPD 無効デプロイでも DBMS_LOCK を
    人間が付与すれば demo が 503 にならない)。VPD 固有定義と query user への setter EXECUTE は
    vpd_enabled のときだけ。権限が無ければ失敗し、lease/integrity ゲートが fail-closed に落とす。"""
    s = get_settings()
    with connect() as conn:
        cur = conn.cursor()
        for ddl in lock_definitions():
            cur.execute(ddl)
        if s.vpd_enabled:
            for ddl in vpd_definitions():
                cur.execute(ddl)
            cur.execute(f"GRANT EXECUTE ON {CTX_PACKAGE} TO {s.adb_query_user}")
        conn.commit()


def apply_policy(cur, table: str) -> None:
    """新規 dataset 表へのポリシー付与(CREATE TABLE → 本関数 → GRANT の順で呼ぶ)。

    ADD_POLICY 失敗時は呼び出し側が GRANT せず表を DROP して失敗を返す契約(specs/18 §4.3)。
    ORA-28101(既存)のみ冪等成功扱い。
    """
    try:
        cur.execute(
            f"""BEGIN
              DBMS_RLS.ADD_POLICY(
                object_schema => '{_schema()}', object_name => :t,
                policy_name => '{POLICY_NAME}', function_schema => '{_schema()}',
                policy_function => '{POLICY_FUNCTION}', statement_types => 'select');
            END;""",
            t=table,
        )
    except Exception as e:
        if "ORA-28101" in str(e):  # policy already exists
            return
        raise


def policy_exists(cur, table: str) -> bool:
    """期待どおりの VPD ポリシー(関数所有者・関数名・SELECT 適用・有効)が付いているか。

    件数だけでなく形を検証する(codex review-2 major — 同名だが permissive/誤設定の
    policy でゲートを開けない)。USER_POLICIES の pf_owner / function / sel / enable を照合。
    """
    cur.execute(
        "SELECT pf_owner, function, sel, enable FROM user_policies "
        "WHERE object_name = :t AND policy_name = :p",
        t=table, p=POLICY_NAME,
    )
    rows = cur.fetchall()
    if len(rows) != 1:
        return False
    pf_owner, function, sel, enable = rows[0]
    return (pf_owner == _schema() and function == POLICY_FUNCTION
            and sel == "YES" and enable == "YES")


def _policy_function_valid(cur) -> bool:
    """ポリシー関数・setter パッケージ・cover package が実在し VALID か(VPD 配備済みか)。"""
    cur.execute(
        "SELECT object_name, status FROM user_objects "
        "WHERE object_name IN (:f, :c) AND object_type IN ('FUNCTION', 'PACKAGE')",
        f=POLICY_FUNCTION, c=CTX_PACKAGE,
    )
    got = {name: status for name, status in cur.fetchall()}
    return (got.get(POLICY_FUNCTION) == "VALID" and got.get(CTX_PACKAGE) == "VALID")


def apply_policies_to_existing() -> list[str]:
    """既存 JETUSE_DS_* 表への一括付与(初回セットアップの一部 — 人間承認のうえ実行)。

    途中失敗しても再実行で収束(付与済みはスキップ)。付与した表名を返す。
    VPD 無効時は何もしない(Public 互換)。
    """
    if not get_settings().vpd_enabled:
        return []
    applied: list[str] = []
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT table_name FROM user_tables "
            "WHERE table_name LIKE 'JETUSE\\_DS\\_%' ESCAPE '\\'"
        )
        for (table,) in cur.fetchall():
            if not policy_exists(cur, table):
                apply_policy(cur, table)
                applied.append(table)
    return applied


# --- 起動時完全性検証(fail-closed ゲート) ---


def verify_integrity() -> list[str]:
    """完全性の問題一覧を返す(空 = 健全)。順序: creating 残骸の reconcile → 検証。

    VPD 未配備でも dataset 表/GRANT 済み表が無ければ健全(Public 互換 — VPD は Internal 機能。
    Public への展開はリリース線別 PR の residual)。表があるのに VPD 未配備なら fail-closed。
    """
    from . import datasets  # 遅延 import(datasets → vpd の循環回避)

    if not get_settings().vpd_enabled:
        return []  # VPD 無効(Public/main 互換): 行レベル分離を強制しない(codex review-10 B004)

    problems: list[str] = []
    qry_user = get_settings().adb_query_user.upper()
    with connect() as conn:
        cur = conn.cursor()
        # 旧登録簿(STATE 列なし)を先に移行してから state を参照する(SELECT state が
        # ORA-00904 で verify/整合ゲートを恒久停止させない — codex review-10 B001)。
        if ddl_verify.table_exists(cur, "JETUSE_DATASETS"):
            datasets._ensure_meta(cur)
        # 実在からの列挙: JETUSE_DS_* 全表 ∪ query user へ SELECT 付与済みの全オブジェクト
        cur.execute(
            "SELECT table_name FROM user_tables "
            "WHERE table_name LIKE 'JETUSE\\_DS\\_%' ESCAPE '\\'"
        )
        ds_tables = {r[0] for r in cur.fetchall()}
        cur.execute(
            "SELECT table_name FROM user_tab_privs "
            "WHERE grantee = :g AND privilege = 'SELECT'",
            g=qry_user,
        )
        granted = {r[0] for r in cur.fetchall()}
        targets = ds_tables | granted

        vpd_deployed = _policy_function_valid(cur)
        if not vpd_deployed:
            # VPD 未配備: 保護対象の表が無ければ健全(互換)、あれば fail-closed
            if targets:
                problems.append(
                    f"VPD not deployed but {len(targets)} dataset table(s) exist "
                    "(deploy VPD via ops/setup-vpd.py — human gate)"
                )
            return problems

        # 以降は VPD 配備済み。creating 残骸を先に回収してから検証する
        try:
            datasets.reconcile_creating()  # CREATE 直後クラッシュの VPD なし表を回収
        except Exception as e:  # noqa: BLE001
            problems.append(f"creating reconcile failed: {str(e)[:200]}")

        # 機能プローブ: コンテキスト・setter の実在(app セッションで set/clear)
        try:
            cur.callproc(f"{CTX_PACKAGE}.set_owner", ["__integrity_probe__"])
            cur.execute(f"SELECT SYS_CONTEXT('{context_name()}', 'OWNER_KEY') FROM dual")
            if cur.fetchone()[0] != "__integrity_probe__":
                problems.append("context probe: value not set")
            cur.callproc(f"{CTX_PACKAGE}.clear_owner", [])
        except Exception as e:  # noqa: BLE001
            problems.append(f"context/setter probe failed: {str(e)[:200]}")

        cur.execute(
            "SELECT COUNT(*) FROM user_tables WHERE table_name = 'JETUSE_DATASETS'"
        )
        has_registry = cur.fetchone()[0] > 0
        for table in sorted(targets):
            states: list[str] = []
            if has_registry:
                cur.execute(
                    "SELECT state FROM JETUSE_DATASETS WHERE table_name = :t", t=table
                )
                states = [r[0] for r in cur.fetchall()]
            if len(states) != 1:
                problems.append(f"{table}: registry rows = {len(states)} (期待 1)")
                continue
            if states[0] == "creating" and table not in granted:
                # CREATE 直後クラッシュの残骸(GRANT 前 = 構造的に不可視)。
                # reconcile が回収する — 完全性違反にすると回収経路が到達不能になる
                # (specs/18 §4.3 codex review-12 M001)
                continue
            if not policy_exists(cur, table):
                problems.append(f"{table}: VPD policy missing")
    return problems


def integrity_gate() -> None:
    """dbchat / datasets 経路の入口で呼ぶ。健全が確認されるまで 503(fail-closed)。

    肯定結果はプロセス内キャッシュ(以後の呼び出しは無コスト)。否定は毎回再検証
    (整理後に再起動なしで解除される)。
    """
    global _integrity_ok
    if _integrity_ok:
        return
    problems = verify_integrity()
    if problems:
        logger.error("VPD integrity check failed: %s", "; ".join(problems)[:500])
        raise DatasetsSecurityError(
            f"datasets security boundary incomplete ({len(problems)} problems)"
        )
    _integrity_ok = True


# --- コンテキスト set/clear 契約(プール接続は再利用される — specs/18 §4.3) ---


def set_owner_context(conn, owner_key: str) -> None:
    """query user 接続を取得するたび、SQL の parse 前に必ずそのリクエストの owner で上書き。
    設定失敗時は SQL を実行しない(例外がそのまま伝播)。VPD 無効時は setter が存在しないため
    何もしない(Public 互換 — 分離なし。codex review-10 B004)。"""
    if not get_settings().vpd_enabled:
        return
    conn.cursor().callproc(f"{_schema()}.{CTX_PACKAGE}.set_owner", [owner_key])


def clear_owner_context(conn) -> None:
    """finally で必ず呼んでから接続を返却する(コンテキスト残留の越境を防ぐ)。VPD 無効は no-op。"""
    if not get_settings().vpd_enabled:
        return
    conn.cursor().callproc(f"{_schema()}.{CTX_PACKAGE}.clear_owner", [])
