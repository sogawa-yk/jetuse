"""Platform API ブローカー — 認可コアのスパイク(PAPI-01 / ADR-0014)。

plan §7 / D5: L2 コネクタ・L3 ホスト型アプリ・生成デモが **DB 認証情報を持たずに**テナントデータへ
到達する唯一の正規経路。プラグインはブローカーが発行する **スコープ付き短期トークン** を提示し、
ブローカーがスコープ・テナント境界・監査を一元的に強制する。

本モジュールは **スパイク** であり、認可基盤の配管(発行/検証/スコープ強制/テナント一致/監査)に
限定する:
  - 実 Platform API ルート本体(rag.search/db.query/conversations/files/connector.invoke)は
    **PAPI-03**。
  - スコープ承認 UI・発行フローの本実装は **PAPI-02**。
  - OIDC による発行主体認証は INFRA-02。

設計の正本は docs/decisions/ADR-0014。スコープ語彙は manifest の `PLATFORM_SCOPES` を**唯一の正本**
として再利用し(manifest `permissions` と発行トークンの `scope` が必ず突き合う)、乖離を作らない。

セキュリティ姿勢: **fail-closed**。署名不正・期限切れ・nbf 未到来・iss/aud 不一致・未知スコープ・
tenant 欠落・鍵未設定・その他あらゆる失敗を、例外を漏らさず「不可(BrokerDenied)」に倒す
(manifest.verify_signature と同方針)。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

from .plugins.manifest import PLATFORM_SCOPES
from .settings import Settings, get_settings

logger = logging.getLogger("jetuse.platform_broker")

# --- 定数(ADR-0014 の正本) ------------------------------------------------

#: 発行者・受け手の固定値。検証時にこの一致を必須にする(取り違え防止)。
ISSUER = "jetuse-platform-broker"
AUDIENCE = "jetuse-platform-api"
#: トークン署名アルゴリズム。発行=検証が JetUse 内で閉じるため対称鍵 HS256(ADR-0014 §2)。
#: 将来 L3 へ検証を委譲するなら非対称へ差し替える(発行/検証は alg を一点に集約してある)。
ALGORITHM = "HS256"
#: TTL 上限(秒)。これを超える発行要求は拒否する(短期トークンの前提を破らせない)。
MAX_TTL_SECONDS = 900


class BrokerConfigError(RuntimeError):
    """ブローカー署名鍵(platform_broker_secret)が未設定。発行・検証とも不可(fail-closed)。"""


@dataclass(frozen=True)
class BrokerDenied(Exception):
    """アクセス拒否。reason は機械可読、message は人間可読。監査に DENY として残す。"""

    reason: str
    message: str = ""

    def __str__(self) -> str:  # pragma: no cover - 表示用
        return self.message or self.reason


def _secret(settings: Settings) -> str:
    secret = settings.platform_broker_secret
    if not secret:
        # 鍵が無い環境では安全側に閉じる(誰の署名も作れず誰の署名も信じない)。
        raise BrokerConfigError(
            "platform_broker_secret が未設定。ブローカーは fail-closed(発行・検証とも不可)"
        )
    return secret


def _now(now: datetime | None) -> datetime:
    # tz-aware UTC を強制する(naive datetime の比較事故を防ぐ)。
    if now is None:
        return datetime.now(UTC)
    if now.tzinfo is None:
        return now.replace(tzinfo=UTC)
    return now.astimezone(UTC)


def _normalize_scopes(scopes: Iterable[str]) -> frozenset[str]:
    """スコープ集合を検証して正規化する。未知スコープ・空はここで弾く(発行側の前倒し検出)。"""
    result = frozenset(scopes)
    if not result:
        raise BrokerDenied("empty_scope", "付与スコープが空。最低 1 つ必要")
    unknown = result - PLATFORM_SCOPES
    if unknown:
        raise BrokerDenied(
            "unknown_scope", f"未知スコープ: {sorted(unknown)}(PLATFORM_SCOPES の部分集合のみ)"
        )
    return result


# --- 発行(PAPI-02 が承認済みスコープを渡してここを呼ぶ) ----------------------


def issue_broker_token(
    plugin_id: str,
    tenant: str,
    scopes: Iterable[str],
    *,
    settings: Settings | None = None,
    ttl_seconds: int | None = None,
    now: datetime | None = None,
) -> str:
    """テナント・プラグイン・付与スコープを内包する短期 JWT を発行する(ADR-0014 §2)。

    scopes は `PLATFORM_SCOPES` の部分集合のみ(未知スコープは拒否)。ttl は MAX_TTL_SECONDS 以内。
    plugin_id / tenant は空にできない(同定とテナント境界の前提)。
    """
    settings = settings or get_settings()
    secret = _secret(settings)
    if not (plugin_id and plugin_id.strip()):
        raise BrokerDenied("missing_plugin", "plugin_id が空")
    if not (tenant and tenant.strip()):
        raise BrokerDenied("missing_tenant", "tenant(Project OCID)が空")

    granted = _normalize_scopes(scopes)
    ttl = settings.platform_token_ttl_seconds if ttl_seconds is None else ttl_seconds
    if ttl <= 0:
        raise BrokerDenied("bad_ttl", "ttl は正の秒数")
    if ttl > MAX_TTL_SECONDS:
        raise BrokerDenied("bad_ttl", f"ttl は {MAX_TTL_SECONDS} 秒以内(短期トークンの前提)")

    issued = _now(now)
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": plugin_id,
        "tenant": tenant,
        # OAuth2 慣習のスペース区切り。順序を固定して再現性を持たせる。
        "scope": " ".join(sorted(granted)),
        "jti": uuid.uuid4().hex,
        "iat": issued,
        "nbf": issued,
        "exp": issued + timedelta(seconds=ttl),
    }
    return jwt.encode(claims, secret, algorithm=ALGORITHM)


# --- 検証(fail-closed) ----------------------------------------------------


@dataclass(frozen=True)
class BrokerContext:
    """検証済みトークンの文脈。スコープ強制・テナント一致の判断はここを起点にする。"""

    plugin_id: str
    tenant: str
    scopes: frozenset[str]
    jti: str
    expires_at: datetime

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    def require_scope(self, scope: str) -> None:
        """スコープ不足なら BrokerDenied。呼び出しごとに必要スコープを 1 つ強制する。"""
        if scope not in self.scopes:
            raise BrokerDenied(
                "scope_denied", f"スコープ '{scope}' が未付与(付与: {sorted(self.scopes)})"
            )

    def require_tenant(self, tenant: str) -> None:
        """要求リソースのテナントとトークンの tenant が一致しなければ拒否(越境防止)。"""
        if tenant != self.tenant:
            raise BrokerDenied(
                "tenant_mismatch",
                f"テナント越境: トークン tenant={self.tenant} だが要求 tenant={tenant}",
            )


def verify_broker_token(
    token: str,
    *,
    settings: Settings | None = None,
) -> BrokerContext:
    """短期トークンを検証して BrokerContext を返す。**fail-closed**。

    署名不正・期限切れ・nbf 未到来・iss/aud 不一致・未知スコープ混入・tenant/sub/jti 欠落・その他
    あらゆる失敗は BrokerDenied に倒す。鍵未設定(BrokerConfigError)はそのまま送出する(運用設定の不備
    として握りつぶさず可視化する。アクセス自体は当然不可)。

    時刻は PyJWT が exp/nbf/iat を実時刻で検証する。発行を過去/未来時刻に寄せたテストは
    issue_broker_token(now=...) で exp/nbf をトークンに焼き込んで再現する(検証側に now 注入は不要)。
    """
    settings = settings or get_settings()
    secret = _secret(settings)
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=[ALGORITHM],
            audience=AUDIENCE,
            issuer=ISSUER,
            options={
                # tenant/scope/jti も必須にする。jti 欠落を許可して監査 jti が空になる穴を塞ぐ
                # (ADR-0014: jti は監査・将来失効の継ぎ目で常在が前提)。
                "require": ["exp", "iat", "nbf", "iss", "aud", "sub", "tenant", "scope", "jti"],
                "verify_aud": True,
                "verify_iss": True,
            },
        )
    except jwt.PyJWTError as e:
        # 署名/期限/nbf/iss/aud/必須欠落 等すべてここに集約して不可に倒す。
        raise BrokerDenied("invalid_token", f"トークン検証失敗: {type(e).__name__}") from e

    tenant = claims.get("tenant")
    plugin_id = claims.get("sub")
    jti = claims.get("jti")
    if not tenant or not isinstance(tenant, str):
        raise BrokerDenied("missing_tenant", "トークンに tenant(Project OCID)が無い")
    if not plugin_id or not isinstance(plugin_id, str):
        raise BrokerDenied("missing_plugin", "トークンに sub(plugin_id)が無い")
    if not jti or not isinstance(jti, str):
        raise BrokerDenied("missing_jti", "トークンに jti(監査・失効の継ぎ目)が無い")

    raw_scope = claims.get("scope", "")
    scope_set = frozenset(s for s in str(raw_scope).split() if s)
    if not scope_set:
        raise BrokerDenied("empty_scope", "トークンに有効なスコープが無い")
    unknown = scope_set - PLATFORM_SCOPES
    if unknown:
        # 検証を迂回して鋳造された(あるいは語彙が退役した)未知スコープは信じない。
        raise BrokerDenied("unknown_scope", f"トークンに未知スコープ: {sorted(unknown)}")

    return BrokerContext(
        plugin_id=plugin_id,
        tenant=tenant,
        scopes=scope_set,
        jti=jti,
        expires_at=datetime.fromtimestamp(int(claims["exp"]), tz=UTC),
    )


# --- 単一の仲介エントリポイント(検証 + スコープ + テナント + 監査) --------------


def authorize(
    token: str,
    required_scope: str,
    *,
    tenant: str,
    resource: str = "",
    settings: Settings | None = None,
    audit: bool = True,
) -> BrokerContext:
    """ブローカーの仲介本体: トークン検証 → スコープ強制 → テナント一致 を一度に行う。

    実 API ルート(PAPI-03)は各エンドポイントの冒頭でこれを呼び、返った BrokerContext の範囲でだけ
    テナントデータへ触れる。許可・拒否のいずれも `platform_broker_audit` に記録する(越境試行を必ず
    残す)。`tenant` は要求リソースが属するテナント(Project OCID)。

    鍵未設定(BrokerConfigError)も DENY として監査に残してから送出する(fail-closed の監査が
    設定不備で穴あきにならないようにする。例外自体は運用に気付かせるため握りつぶさない)。
    """
    settings = settings or get_settings()
    ctx: BrokerContext | None = None
    try:
        # 要求スコープ自体が既知語彙か入口で検証する(発行/検証と同じ fail-closed を仲介にも適用)。
        # PAPI-03 実装でスコープ名を typo しても scope_denied で素通り監査されるだけにせず、
        # 構成ミスとして即座に弾く(ADR-0014「未知スコープは拒否」)。
        if required_scope not in PLATFORM_SCOPES:
            raise BrokerDenied(
                "unknown_scope", f"未知の要求スコープ: {required_scope}(PLATFORM_SCOPES 外)"
            )
        ctx = verify_broker_token(token, settings=settings)
        # tenant 一致を scope より先に検査する。別テナントかつ scope 不足のとき scope_denied で
        # 上書きせず、越境は必ず tenant_mismatch として監査に残す(ADR-0014 §3 の契約)。
        ctx.require_tenant(tenant)
        ctx.require_scope(required_scope)
    except (BrokerDenied, BrokerConfigError) as err:
        if audit:
            # 拒否は本人不明でも残す(検証失敗なら ctx is None)。tenant は要求側を記録。
            reason = err.reason if isinstance(err, BrokerDenied) else "broker_unconfigured"
            record_broker_access(
                plugin_id=ctx.plugin_id if ctx else "?",
                tenant=tenant,
                scope=required_scope,
                decision="DENY",
                reason=reason,
                resource=resource,
                # 検証通過後に scope/tenant で拒否したケースは jti が残る(越境トークンの特定用)。
                jti=ctx.jti if ctx else "",
            )
        raise
    if audit:
        record_broker_access(
            plugin_id=ctx.plugin_id,
            tenant=ctx.tenant,
            scope=required_scope,
            decision="ALLOW",
            reason="",
            resource=resource,
            jti=ctx.jti,
        )
    return ctx


# --- 監査(ベストエフォート。audit.py と同方針) ------------------------------


def record_broker_access(
    *,
    plugin_id: str,
    tenant: str,
    scope: str,
    decision: str,
    reason: str = "",
    resource: str = "",
    jti: str = "",
) -> None:
    """ブローカーアクセス(許可/拒否)を `platform_broker_audit` に記録する。

    記録先はプロセスの DB(`db.connect()` = グローバル設定)。監査は実テナント DB に集約するため
    settings 注入は受けない(connect は設定注入を取らない)。
    ベストエフォート: 記録失敗はログのみでサービスを止めない(audit.log_event と同方針)。
    越境の試行(DENY)が必ず監査に残ることを保証するのが狙い(plan §12)。
    """
    from .db import connect

    try:
        with connect() as conn:
            conn.cursor().execute(
                """
                INSERT INTO platform_broker_audit(
                    id, tenant, plugin_id, scope, decision, reason, resource_id, jti)
                VALUES (:id, :tenant, :plugin, :scope, :decision, :reason, :res, :jti)
                """,
                id=uuid.uuid4().hex,
                tenant=(tenant or "")[:255],
                plugin=(plugin_id or "")[:255],
                scope=(scope or "")[:64],
                decision=(decision or "")[:8],
                reason=(reason or "")[:200] or None,
                # bind 名は :res(:resource は予約語 RESOURCE 扱いで ORA-01745 になる)。
                res=(resource or "")[:255] or None,
                jti=(jti or "")[:64] or None,
            )
            conn.commit()
    except Exception:
        logger.exception("platform broker audit failed (ignored)")
