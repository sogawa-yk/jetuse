"""Platform API スコープ承認＋短期トークン発行フロー(PAPI-02 / ADR-0014)。

PAPI-01 の認可コア(`platform_broker.py`: 発行/検証/スコープ強制/テナント境界/監査)の**上の層**。
本モジュールは ADR-0014 §2「スコープは manifest `permissions` 由来で、インストール／合成時に
承認された範囲だけを載せる(PAPI-02)」を実装する:

  1. **スコープ承認(approve_scopes)**: 人間=SA が、プラグインが manifest で要求した
     `permissions` の範囲内で、テナント(Project OCID)ごとに承認スコープを確定し
     `platform_scope_grants` に永続化する。承認は (tenant, plugin_id) で upsert(再承認で更新)、
     失効は revoke_grant(status=REVOKED)。
  2. **発行フロー(issue_token)**: 承認済みグラントを読み、**承認スコープに厳密に閉じた**短期 JWT を
     `platform_broker.issue_broker_token` 経由で発行する。グラント無し・失効・承認超過要求は
     **トークンを発行せず**拒否する(fail-closed)。

責務境界(PAPI-01 と分離):
  - トークンの暗号的な発行/検証/スコープ強制/テナント一致/監査は `platform_broker.py`(認可コア)。
  - 本モジュールは「どのスコープを承認し、発行時に何を載せてよいか」という承認ポリシーと
    永続化に限定する。
  - 実 Platform API ルート本体(rag.search/db.query 等)は PAPI-03。承認 UI の画面は後続。

発行粒度(ADR-0014 §2 の委任を本タスクで確定): **呼び出しごと**(issue_token は呼ばれるたびに
新規 JWT を発行する)。TTL 内の単回使用強制(jti 消費)を持たない MVP では、粒度を細かくするほど
リプレイ露出窓が小さくなる(ADR-0014 §2)。よってセッション単位で使い回さず、呼び出しごとに発行する。

セキュリティ姿勢: **fail-closed**(`platform_broker.py` と同方針)。承認・発行のあらゆる不確かさを
「不可」に倒す。**トークン署名鍵(platform_broker_secret)・DB 認証情報・実シークレット値はグラント
行・トークンのいずれにも保存しない**(ADR-0014 / CLAUDE.md)。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import oracledb

from . import platform_broker as pb
from .plugins.manifest import (
    MAX_ID_LEN,
    MAX_VERSION_LEN,
    PLATFORM_SCOPES,
    PluginManifest,
)
from .settings import Settings

# グラント状態。ACTIVE のみが発行に使える。REVOKED は履歴・監査追跡のために行を残す。
GRANT_STATUS_ACTIVE = "ACTIVE"
GRANT_STATUS_REVOKED = "REVOKED"

# DB カラム幅(VARCHAR2)と一致させる入力上限。超えると保存時に ORA-12899 になるため、書き込み境界で
# 弾いて予測可能な ValueError にする(connector_store.py / scaffold.py と同じ方針)。
MAX_APPROVED_BY_LEN = 255
# tenant 列幅(Project OCID)。OCID は十分短いが保存前に弾いて予測可能にする。
MAX_TENANT_LEN = 255


class GrantError(ValueError):
    """承認の入力が不正(未知/非要求/空スコープ・空 tenant 等)。承認は成立させない(fail-closed)。"""


@dataclass(frozen=True)
class GrantDenied(Exception):
    """発行フローの拒否(グラント無し/失効/承認超過要求)。トークンは発行しない(fail-closed)。

    reason は機械可読、message は人間可読。`platform_broker.BrokerDenied` と同じ形にして、発行経路
    全体(承認層＋認可コア)で拒否の扱いを揃える。
    """

    reason: str
    message: str = ""

    def __str__(self) -> str:  # pragma: no cover - 表示用
        return self.message or self.reason


# --- 純粋な承認ポリシー(DB 非依存。単体テストの主対象) ------------------------


def validate_grant_scopes(
    manifest: PluginManifest, scopes: Iterable[str]
) -> frozenset[str]:
    """承認しようとするスコープを検証して正規化する(DB 非依存・fail-closed)。

    承認可能なのは **manifest が要求した `permissions`** かつ `PLATFORM_SCOPES` の部分集合のみ。
      - 空集合は拒否(空の承認は無意味で、発行時に空スコープを生む穴になる)。
      - 未知スコープ(語彙外)は拒否。
      - manifest が要求していないスコープの承認は拒否(プラグインが宣言していない権限を後付けで
        与えない＝最小権限。manifest.permissions が正本)。
    """
    requested = frozenset(scopes)
    if not requested:
        raise GrantError("承認スコープが空。最低 1 つ必要(empty_scope)")
    unknown = requested - PLATFORM_SCOPES
    if unknown:
        raise GrantError(
            f"未知スコープ: {sorted(unknown)}(PLATFORM_SCOPES の部分集合のみ)"
        )
    declared = frozenset(manifest.permissions)
    not_requested = requested - declared
    if not_requested:
        raise GrantError(
            f"manifest が要求していないスコープは承認できない: {sorted(not_requested)}"
            f"(manifest.permissions={sorted(declared)})"
        )
    return requested


def select_issuable_scopes(
    granted: frozenset[str], requested: Iterable[str] | None
) -> frozenset[str]:
    """発行トークンに載せるスコープを決める(DB 非依存・fail-closed)。

    requested=None なら承認スコープ全体を載せる。明示要求があるときは **承認スコープの部分集合のみ**
    許可し、超過(`scope_not_granted`)・空要求は拒否する。承認を超えるスコープは決して載せない。
    """
    if requested is None:
        return granted
    want = frozenset(requested)
    if not want:
        raise GrantDenied("empty_request", "要求スコープが空")
    excess = want - granted
    if excess:
        raise GrantDenied(
            "scope_not_granted",
            f"承認外スコープの要求: {sorted(excess)}(承認: {sorted(granted)})",
        )
    return want


# --- 承認の永続化(platform_scope_grants) -------------------------------------

_GRANT_COLS = (
    "id, tenant, plugin_id, source_version, scopes, status, "
    "approved_by, created_at, updated_at"
)


def _ts(v: Any) -> Any:
    return v.isoformat() if hasattr(v, "isoformat") else v


def _grant_row_to_record(r) -> dict[str, Any]:
    return {
        "id": r[0],
        "tenant": r[1],
        "plugin_id": r[2],
        "source_version": r[3],
        # 保存はスペース区切り。取り出しはソート済みリストで決定的に返す。
        "scopes": sorted(s for s in str(r[4]).split() if s),
        "status": r[5],
        "approved_by": r[6],
        "created_at": _ts(r[7]),
        "updated_at": _ts(r[8]),
    }


def approve_scopes(
    manifest: PluginManifest,
    *,
    tenant: str,
    scopes: Iterable[str],
    approved_by: str,
) -> dict[str, Any]:
    """プラグインへ tenant 単位でスコープを承認し、グラント記録(upsert 後)を返す。

    承認スコープは `validate_grant_scopes` で **manifest.permissions ∩ PLATFORM_SCOPES** に閉じる。
    (tenant, plugin_id) で一意 = 既存があれば更新(再承認: scopes/source_version/approved_by を
    差し替え、status を ACTIVE へ戻す)。トークン署名鍵・実シークレットは保存しない。
    """
    if not isinstance(manifest, PluginManifest):
        raise GrantError("manifest は PluginManifest でなければならない")
    if not (tenant and tenant.strip()):
        raise GrantError("tenant(Project OCID)は非空でなければならない")
    # tenant は一意キーかつ issue_token の検索キー。前後空白付きを許すと同一 OCID でも別グラントに
    # 割れ、正規化済み tenant で引くと no_grant になる。書込前に前後空白を拒否し割れを防ぐ(F-002)。
    if tenant != tenant.strip():
        raise GrantError("tenant に前後の空白を含めてはならない(正規化済みの OCID を渡すこと)")
    if len(tenant) > MAX_TENANT_LEN:
        raise GrantError(f"tenant は {MAX_TENANT_LEN} 文字以内でなければならない")
    if not (approved_by and approved_by.strip()):
        raise GrantError("approved_by は非空でなければならない")
    if approved_by != approved_by.strip():
        raise GrantError("approved_by に前後の空白を含めてはならない")
    if len(approved_by) > MAX_APPROVED_BY_LEN:
        raise GrantError(
            f"approved_by は {MAX_APPROVED_BY_LEN} 文字以内でなければならない"
        )
    # manifest.id/version は manifest 側で MAX_ID_LEN/MAX_VERSION_LEN に収まる検証済みだが、
    # 念のため境界を明示しておく(列幅と一致。検証済み manifest は必ず保存できる)。
    if len(manifest.id) > MAX_ID_LEN or len(manifest.version) > MAX_VERSION_LEN:
        raise GrantError("manifest の id/version が列幅を超えている")

    granted = validate_grant_scopes(manifest, scopes)
    # OAuth2 慣習のスペース区切り。順序を固定して再現性を持たせる(broker の scope claim と同方針)。
    scopes_str = " ".join(sorted(granted))

    from .db import connect

    new_id = str(uuid.uuid4())
    with connect() as conn:
        cur = conn.cursor()
        # MERGE で upsert: 既存(tenant, plugin_id)があれば更新、無ければ挿入。
        # 更新時は created_at を保持し updated_at だけ進める。再承認で status を ACTIVE に戻す。
        cur.execute(
            """
            MERGE INTO platform_scope_grants g
            USING (SELECT :tenant AS tenant, :pid AS plugin_id FROM dual) s
            ON (g.tenant = s.tenant AND g.plugin_id = s.plugin_id)
            WHEN MATCHED THEN UPDATE SET
              g.source_version = :ver,
              g.scopes = :scopes,
              g.status = :active,
              g.approved_by = :approver,
              g.updated_at = SYSTIMESTAMP
            WHEN NOT MATCHED THEN INSERT
              (id, tenant, plugin_id, source_version, scopes, status, approved_by)
            VALUES
              (:id, :tenant, :pid, :ver, :scopes, :active, :approver)
            """,
            id=new_id,
            tenant=tenant,
            pid=manifest.id,
            ver=manifest.version,
            scopes=scopes_str,
            active=GRANT_STATUS_ACTIVE,
            # bind 名は予約語を避ける(store.py の installer 教訓に倣う)。
            approver=approved_by,
        )
        conn.commit()
        rec = _select_grant(cur, tenant, manifest.id)
    if rec is None:  # pragma: no cover - MERGE 直後に必ず取得できる
        raise GrantError("承認の保存後にグラントを取得できなかった")
    return rec


def _select_grant(cur, tenant: str, plugin_id: str) -> dict[str, Any] | None:
    cur.execute(
        f"""
        SELECT {_GRANT_COLS} FROM platform_scope_grants
        WHERE tenant = :tenant AND plugin_id = :pid
        """,
        tenant=tenant,
        pid=plugin_id,
    )
    row = cur.fetchone()
    return _grant_row_to_record(row) if row else None


def get_grant(tenant: str, plugin_id: str) -> dict[str, Any] | None:
    """(tenant, plugin_id) の承認グラントを取得する。無ければ None。"""
    from .db import connect

    with connect() as conn:
        return _select_grant(conn.cursor(), tenant, plugin_id)


def list_grants(
    tenant: str | None = None,
    plugin_id: str | None = None,
    *,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """承認グラントを新しい順に一覧する。tenant / plugin_id / status で絞り込める。"""
    from .db import connect

    where = []
    binds: dict[str, Any] = {}
    if tenant is not None:
        where.append("tenant = :tenant")
        binds["tenant"] = tenant
    if plugin_id is not None:
        where.append("plugin_id = :pid")
        binds["pid"] = plugin_id
    if status is not None:
        where.append("status = :status")
        binds["status"] = status
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT {_GRANT_COLS} FROM platform_scope_grants{clause}
            ORDER BY updated_at DESC FETCH FIRST 500 ROWS ONLY
            """,
            **binds,
        )
        return [_grant_row_to_record(r) for r in cur.fetchall()]


def revoke_grant(tenant: str, plugin_id: str) -> bool:
    """承認を失効させる(status=REVOKED)。失効対象の ACTIVE 行があれば True。

    行は残す(監査・履歴追跡のため)。失効後の issue_token は `grant_revoked` で発行を拒否する。
    既に REVOKED の行は対象外(冪等に False を返す)。
    """
    from .db import connect

    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE platform_scope_grants
            SET status = :revoked, updated_at = SYSTIMESTAMP
            WHERE tenant = :tenant AND plugin_id = :pid AND status = :active
            """,
            revoked=GRANT_STATUS_REVOKED,
            active=GRANT_STATUS_ACTIVE,
            tenant=tenant,
            pid=plugin_id,
        )
        changed = cur.rowcount
        conn.commit()
    return changed > 0


def revoke_grant_capture(tenant: str, plugin_id: str) -> list[str] | None:
    """ACTIVE グラントを失効し、**失効した scope 集合**を同一トランザクションで原子的に返す。

    失効対象が無ければ None。`get_grant` → `revoke_grant` の 2 段だと、その間に同じ
    (tenant, plugin_id) が再承認されると「監査に載せる scope」と「実際に失効した scope」がずれる
    (再承認との競合 / 監査の取り違え)。UPDATE ... RETURNING で**実際に失効した行**の scope を
    取り出し、監査が常に実失効と一致するようにする。承認 UI からの失効はこの原子操作を使う
    (`revoke_grant` は冪等な真偽判定用に残す)。
    """
    from .db import connect

    with connect() as conn:
        cur = conn.cursor()
        # DML RETURNING は影響行ごとに値を返す。単一行 UPDATE なので 0 or 1 件。
        out = cur.var(oracledb.DB_TYPE_VARCHAR)
        cur.execute(
            """
            UPDATE platform_scope_grants
            SET status = :revoked, updated_at = SYSTIMESTAMP
            WHERE tenant = :tenant AND plugin_id = :pid AND status = :active
            RETURNING scopes INTO :out
            """,
            revoked=GRANT_STATUS_REVOKED,
            active=GRANT_STATUS_ACTIVE,
            tenant=tenant,
            pid=plugin_id,
            out=out,
        )
        changed = cur.rowcount
        conn.commit()
    if changed == 0:
        return None
    vals = out.getvalue() or []
    raw = vals[0] if vals else ""
    # 保存はスペース区切り。取り出しはソート済みで決定的に返す(_grant_row_to_record と同方針)。
    return sorted(s for s in str(raw or "").split() if s)


# --- 発行フロー(承認に閉じた短期トークン) ------------------------------------


def issue_token(
    tenant: str,
    plugin_id: str,
    *,
    scopes: Iterable[str] | None = None,
    settings: Settings | None = None,
    ttl_seconds: int | None = None,
) -> str:
    """承認済みグラントに**厳密に閉じた**短期 JWT を発行する(発行フローの本体)。

    手順(fail-closed):
      1. (tenant, plugin_id) の承認グラントを読む。無ければ `no_grant` で拒否(トークン未発行)。
      2. status が ACTIVE でなければ `grant_revoked` で拒否。
      3. 載せるスコープを決める(`scopes=None` なら承認全体、明示要求は承認の部分集合のみ)。
         承認を超える要求は `scope_not_granted` で拒否。
      4. `platform_broker.issue_broker_token` で署名・TTL を付けて発行する(認可コアに委譲)。
         語彙が退役したスコープが承認に残っていても broker 側が未知スコープとして弾く(fail-closed)。

    発行粒度は**呼び出しごと**(ADR-0014 §2 の委任を確定)。セッションで使い回さない。
    """
    grant = get_grant(tenant, plugin_id)
    if grant is None:
        raise GrantDenied(
            "no_grant",
            f"承認グラントが無い(tenant={tenant}, plugin={plugin_id})。先に approve_scopes が必要",
        )
    if grant["status"] != GRANT_STATUS_ACTIVE:
        raise GrantDenied(
            "grant_revoked",
            f"グラントが ACTIVE でない(status={grant['status']})。発行不可",
        )
    granted = frozenset(grant["scopes"])
    issuable = select_issuable_scopes(granted, scopes)
    # 認可コアへ委譲(署名・iss/aud・jti・iat/nbf/exp・TTL 上限・未知スコープ排除はすべて broker)。
    return pb.issue_broker_token(
        plugin_id,
        tenant,
        issuable,
        settings=settings,
        ttl_seconds=ttl_seconds,
    )
