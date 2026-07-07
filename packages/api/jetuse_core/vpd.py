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
import re

from . import ddl_verify
from .db import connect
from .settings import get_settings

logger = logging.getLogger("jetuse.vpd")

POLICY_NAME = "JETUSE_DS_POLICY"
CTX_PACKAGE = "JETUSE_VPD_CTX"
POLICY_FUNCTION = "JETUSE_VPD_POLICY"
LOCK_PACKAGE = "JETUSE_LOCK"

_IDENT_RE = re.compile(r"[A-Z][A-Z0-9_$#]*\Z")


def _assert_ident(name: str) -> str:
    """未引用 Oracle 識別子として正規化・検証(DDL へ interpolate する前の注入防止)。"""
    up = (name or "").strip().upper()
    if not (0 < len(up) <= 128 and _IDENT_RE.match(up)):
        raise ValueError(f"invalid Oracle identifier: {name!r}")
    return up

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
    """排他リース cover package(specs/18 §3.2.1 — ALLOCATE_UNIQUE/REQUEST/RELEASE のみ晒す最小案）。

    Gate 2 最小カバーパッケージ案(人間承認 2026-07-07): ADMIN 所有(definer's rights)で作成し、
    アプリスキーマには DBMS_LOCK 直付けでなく EXECUTE + private synonym のみ付与する
    (provision_lock_for)。ALLOCATE_UNIQUE でロック名→一意ハンドルを取るため ORA_HASH 数値 ID の
    衝突が無い。付与前は synonym 解決不能で lease acquire が LeaseUnavailableError=503。
    """
    return [
        f"""CREATE OR REPLACE PACKAGE {LOCK_PACKAGE} AS
  FUNCTION allocate_unique(p_name IN VARCHAR2) RETURN VARCHAR2;
  FUNCTION request(p_handle IN VARCHAR2, p_timeout IN INTEGER) RETURN INTEGER;
  FUNCTION release(p_handle IN VARCHAR2) RETURN INTEGER;
END {LOCK_PACKAGE};""",
        f"""CREATE OR REPLACE PACKAGE BODY {LOCK_PACKAGE} AS
  FUNCTION allocate_unique(p_name IN VARCHAR2) RETURN VARCHAR2 IS
    v_handle VARCHAR2(128);
  BEGIN
    DBMS_LOCK.ALLOCATE_UNIQUE(lockname => p_name, lockhandle => v_handle);
    RETURN v_handle;
  END allocate_unique;
  FUNCTION request(p_handle IN VARCHAR2, p_timeout IN INTEGER) RETURN INTEGER IS
  BEGIN
    RETURN DBMS_LOCK.REQUEST(lockhandle => p_handle, lockmode => DBMS_LOCK.X_MODE,
                             timeout => p_timeout, release_on_commit => FALSE);
  END request;
  FUNCTION release(p_handle IN VARCHAR2) RETURN INTEGER IS
  BEGIN
    RETURN DBMS_LOCK.RELEASE(lockhandle => p_handle);
  END release;
END {LOCK_PACKAGE};""",
    ]


def provision_lock_for(admin_cur, app_schema: str, *, app_offline: bool = False) -> str:
    """ADMIN 接続で最小カバーパッケージ(ADMIN 所有)を作り、app へ EXECUTE + synonym を付与する。

    Gate 2 最小案の実体(人間承認 2026-07-07)。app_schema は DBMS_LOCK を直接 EXECUTE しない。
    新規スキーマ(旧 app 所有 package なし)は無条件に構成する。

    **旧デプロイからの移行は保守ウィンドウ必須(app_offline=True)。** 旧実装の numeric ロック
    (ORA_HASH+DBMS_LOCK.REQUEST(id=>...))と新実装の named ロック(ALLOCATE_UNIQUE handle)は
    別ロック空間で相互排他しない。稼働中に in-place 移行すると旧ワーカーと新ワーカーが同一 demo を
    同時取得し得る(排他崩壊 — codex review-17 blocker)。よって旧 app 所有 JETUSE_LOCK package が
    在るときは、operator がアプリを停止/ドレインしたと明示(app_offline=True)しない限り fail-closed
    で中断する。移行手順: ①全ワーカー停止 → ②app_offline=True で provision → ③新コードで再起動。
    移行時の処理(review-16 major も保持):
    - ADMIN package body が VALID(= ADMIN が DBMS_LOCK を持つ)ことを確認してから旧 app package を
      落とす。不正コンパイルなら中断して既存の稼働リースを壊さない。
    - 旧 app 所有 JETUSE_LOCK package は synonym と名前衝突するので落とす。
    - 旧 direct DBMS_LOCK grant が app に残っていれば REVOKE(最小権限へ収束)。
    返り値 = cover package の所有スキーマ(通常 ADMIN)。
    """
    app_schema = _assert_ident(app_schema)  # DDL interpolate 前の注入防止(review-17 major)
    admin_cur.execute("SELECT USER FROM dual")
    owner = _assert_ident(admin_cur.fetchone()[0])
    for ddl in lock_definitions():
        admin_cur.execute(ddl)  # owner.JETUSE_LOCK(definer's rights)
    # 旧 app package を落とす前に ADMIN 側 body が VALID か確認(ADMIN が DBMS_LOCK 不足なら不正)。
    admin_cur.execute(
        "SELECT status FROM dba_objects WHERE owner = :o AND object_name = :n "
        "AND object_type = 'PACKAGE BODY'", o=owner, n=LOCK_PACKAGE)
    body = admin_cur.fetchone()
    if not body or body[0] != "VALID":
        raise RuntimeError(
            f"{owner}.{LOCK_PACKAGE} body not VALID ({body}) — ADMIN の DBMS_LOCK 権限を確認。"
            f"旧 app package を保持したまま中断する")
    admin_cur.execute(f"GRANT EXECUTE ON {owner}.{LOCK_PACKAGE} TO {app_schema}")
    admin_cur.execute(
        "SELECT COUNT(*) FROM dba_objects WHERE owner = :o AND object_name = :n "
        "AND object_type LIKE 'PACKAGE%'", o=app_schema, n=LOCK_PACKAGE)
    migrating = admin_cur.fetchone()[0] > 0
    if migrating:
        # 旧 numeric ロックと新 named ロックは相互排他しない。稼働中の移行は排他を破るので、
        # app が停止/ドレイン済みだと operator が明示しない限り中断する(fail-closed)。
        if not app_offline:
            raise RuntimeError(
                f"{app_schema} に旧 app 所有 {LOCK_PACKAGE} package が存在。旧 numeric ロックと新 "
                f"named ロックは相互排他しないため live 移行は排他を破る。アプリを停止/ドレインし "
                f"app_offline=True で再実行すること(保守ウィンドウ必須 — review-17 blocker)")
        admin_cur.execute(f"DROP PACKAGE {app_schema}.{LOCK_PACKAGE}")
    admin_cur.execute(
        f"CREATE OR REPLACE SYNONYM {app_schema}.{LOCK_PACKAGE} FOR {owner}.{LOCK_PACKAGE}")
    if migrating:
        # 旧 SYS.DBMS_LOCK direct grant は移行時のみ REVOKE(least-privilege へ収束)。fresh 構成では
        # 既存 grant を一切触らない(他用途の grant を巻き込まない — review-18 major)。
        admin_cur.execute(
            "SELECT COUNT(*) FROM dba_tab_privs WHERE grantee = :g AND owner = 'SYS' "
            "AND table_name = 'DBMS_LOCK' AND privilege = 'EXECUTE'", g=app_schema)
        if admin_cur.fetchone()[0]:
            admin_cur.execute(f"REVOKE EXECUTE ON SYS.DBMS_LOCK FROM {app_schema}")
    return owner


def lock_available() -> bool:
    """アプリ資格情報で JETUSE_LOCK(synonym→ADMIN package もしくは同名 package)が解決可能か。

    起動時の deploy 誤設定可視化用(Gate 2 provision 未実行だと demo 経路が 503 に留まる)。
    """
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM all_synonyms WHERE synonym_name = :n "
            "AND owner IN (USER, 'PUBLIC')", n=LOCK_PACKAGE)
        if cur.fetchone()[0]:
            return True
        cur.execute(
            "SELECT COUNT(*) FROM all_objects WHERE object_name = :n "
            "AND object_type LIKE 'PACKAGE%'", n=LOCK_PACKAGE)
        return cur.fetchone()[0] > 0


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
    """人間承認の対象定義(APPROVAL-REQUEST.md に添付する正本)= 排他リース cover package + VPD。"""
    return lock_definitions() + vpd_definitions()


def reapply_definitions() -> None:
    """VPD 固有定義の冪等再適用(vpd_enabled のときだけ)。

    JETUSE_LOCK cover package は Gate 2 最小案(ADMIN 所有 + app へ synonym/EXECUTE)へ移行したため
    ここでは作らない — app は ADMIN 所有物を作れず DBMS_LOCK 直付けも避けるため
    (provision_lock_for が ADMIN セットアップで用意)。VPD 無効(Public/既定)では no-op。
    権限が無ければ失敗し、integrity ゲートが fail-closed に落とす。"""
    s = get_settings()
    if not s.vpd_enabled:
        return
    with connect() as conn:
        cur = conn.cursor()
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
