"""DEP-02: 生成デモコンテナへの Platform API ランタイム注入(L3 / ADR-0014 / ADR-0015 §7)。

DEP-01(`deploy.py`)は **秘密を一切持たない** 宣言的配備仕様(`ContainerDeploySpec`)を生成する
ところまでを担う。本モジュール(DEP-02)は、その仕様を起点に **コンテナ起動時** の
ランタイム注入を組み立てる:

  - **ベース URL(非秘密 env)**: デモコンテナが Platform API へ到達する URL
    (`JETUSE_PLATFORM_API_BASE_URL`)。
  - **短期トークン(秘密)**: `platform_grants.issue_token` が発行する **承認スコープに厳密に
    閉じた** 短期 JWT(`JETUSE_PLATFORM_TOKEN`)。**呼び出し(=コンテナ起動/更新)ごとに発行**する
    (ADR-0014 §2 / platform_grants の発行粒度)。

設計の核(ADR-0015 §3〜§7 を DEP-02 で確定):
  - **DB 認証情報は注入しない**(D5)。デモコンテナはブローカー発行の短期トークンだけで
    テナントデータへ到達する。注入物は base_url(非秘密)＋ token(秘密)に限る。本モジュールは
    `adb_*` 等の DB 資格を読まない・載せない(構造的に到達不能)。
  - **承認スコープに厳密に閉じる**: トークンに載るスコープは
    **配備仕様 `required_scopes`(デモが宣言した必要スコープ)∩ 承認グラント** に限定する。
    配備仕様が宣言していないスコープは要求できず(deploy-spec 閉包)、承認外スコープは
    `platform_grants.issue_token` が `scope_not_granted` で拒否(grant 閉包)。二重閉包・fail-closed。
  - **秘密と非秘密を分離**: `env()` は **非秘密のみ**(base_url)、トークンは `secret_env()` に分け、
    非秘密 env(committed tfvars 経路)へ秘密を混ぜない(ADR-0015 §3)。

トークンのライフサイクル(ADR-0016 で確定):
  - **TTL は短期**(`settings.platform_token_ttl_seconds`、broker 上限 900 秒)。
    `expires_at` で失効時刻を公開する。
  - **失効(revoke)**: `platform_grants.revoke_grant` 後の再発行(=次回起動/更新)は
    `grant_revoked` で拒否される。実トークンの即時失効機構(jti 失効リスト)は MVP 非対象のため、
    **失効の有効化窓 = TTL**(発行済みは TTL まで有効、その後の更新で必ず止まる)。fail-closed。
  - **更新(refresh)**: 短期 TTL のため、長時間稼働するデモコンテナは TTL 内に **再注入(=本関数の
    再呼び出し)で更新** する。更新時に承認グラントが再評価されるため、失効は TTL 窓内で伝播する。

セキュリティ姿勢: **fail-closed**(deploy.py / platform_broker.py と同方針)。base_url 未設定/不正、
スコープが配備仕様の閉包外/空、グラント無し/失効/承認超過、トークンへの Vault OCID 混入のいずれでも
注入を組み立てない。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from . import platform_broker as pb
from . import platform_grants as pg
from .deploy import _VAULT_OCID_SUBSTR_RE, ContainerDeploySpec
from .settings import Settings, get_settings

# 注入する env キー(契約: hosted-demo Terraform 環境の TF_VAR と一致させる)。
#: 非秘密。Platform API のベース URL。
PLATFORM_API_BASE_URL_ENV = "JETUSE_PLATFORM_API_BASE_URL"
#: 秘密。ブローカー発行の短期 JWT。**非秘密 env には決して載せない**(secret_env 経由)。
PLATFORM_TOKEN_ENV = "JETUSE_PLATFORM_TOKEN"

# ベース URL の形(https 固定・ホスト必須・空白/制御文字なし)。L3 が平文 http で
# 短期トークンを載せて疎通しないよう、スキームは https に固定する(トークン傍受防止)。
_BASE_URL_RE = re.compile(r"^https://[A-Za-z0-9.\-]+(?::\d+)?(?:/[^\s]*)?$")
# ベース URL 長の上限(暴走・異常値防止。deploy.py の上限方針に合わせる)。
MAX_BASE_URL_LEN = 2048

# 注入物に現れてよい env キーの allowlist(多層防御)。これ以外のキーは組み立てない・通さない。
# DB 資格情報名(ADB_*/DB_PASSWORD 等)が紛れ込む経路を構造的に塞ぐ(D5: DB 認証情報は注入しない)。
_ALLOWED_NONSECRET_KEYS = frozenset({PLATFORM_API_BASE_URL_ENV})
_ALLOWED_SECRET_KEYS = frozenset({PLATFORM_TOKEN_ENV})


class InjectionError(ValueError):
    """ランタイム注入を組み立てられない(fail-closed)。base_url 不正・スコープ閉包違反 等。

    グラント無し/失効/承認超過は `platform_grants.GrantDenied`、署名鍵未設定は
    `platform_broker.BrokerConfigError` がそのまま送出される(発行経路の fail-closed を保つ)。
    """


@dataclass(frozen=True)
class RuntimeInjection:
    """L3 デモコンテナ起動時の Platform API 注入バンドル。

    `env()`(非秘密)と `secret_env()`(秘密=トークン)を分けて返す。トークンは **承認スコープに
    厳密に閉じた** 短期 JWT で、`expires_at` まで有効(ADR-0016)。DB 認証情報は一切含まない。
    """

    #: Platform API のベース URL(非秘密)。
    base_url: str
    #: ブローカー発行の短期 JWT(**秘密**)。
    token: str
    #: トークンに実際に載ったスコープ(検証で確定した権威値・ソート済み)。
    scopes: tuple[str, ...]
    #: トークン失効時刻(UTC, tz-aware)。
    expires_at: datetime
    #: テナント(Project OCID)。
    tenant: str
    #: 発行主体プラグイン(承認グラントのキー)。
    plugin_id: str

    def env(self) -> dict[str, str]:
        """コンテナへ渡す **非秘密** env(base_url のみ)。秘密はここに合流させない。"""
        return {PLATFORM_API_BASE_URL_ENV: self.base_url}

    def secret_env(self) -> dict[str, str]:
        """コンテナへ渡す **秘密** env(短期トークンのみ)。

        **Terraform 経由では渡さない**(Terraform に渡した値は resource 入力として state に保存され、
        短期トークンを state へ残してしまうため)。トークンは起動時の **アウトオブバンド注入**
        — オーケストレータが実行中コンテナへ直接注入(MVP)、将来はコンテナ自身がブローカーから取得 —
        で渡す(ADR-0016 §4・§5)。短期 TTL で揮発し、更新は `should_refresh` 判定での再注入で行う。
        """
        return {PLATFORM_TOKEN_ENV: self.token}

    def seconds_remaining(self, now: datetime | None = None) -> int:
        """失効までの残り秒数(0 下限)。更新判断(should_refresh)に使う。"""
        ref = _now(now)
        return max(0, int((self.expires_at - ref).total_seconds()))

    def is_expired(self, now: datetime | None = None) -> bool:
        return self.seconds_remaining(now) <= 0

    def redacted(self) -> dict[str, object]:
        """証跡/ログ用の **トークンを伏せた** 表現(秘密を出力に残さない)。"""
        return {
            "base_url": self.base_url,
            "token": "***redacted***",
            "scopes": list(self.scopes),
            "expires_at": self.expires_at.isoformat(),
            "tenant": self.tenant,
            "plugin_id": self.plugin_id,
        }


def _now(now: datetime | None) -> datetime:
    """tz-aware UTC を強制(platform_broker._now と同方針。naive 比較事故を防ぐ)。"""
    if now is None:
        return datetime.now(UTC)
    if now.tzinfo is None:
        return now.replace(tzinfo=UTC)
    return now.astimezone(UTC)


def _resolve_base_url(base_url: str | None, settings: Settings) -> str:
    """ベース URL を解決・検証する(引数優先、無ければ settings)。fail-closed。

    https 固定・空白/Vault OCID 混入拒否。L3 が平文経路や秘密混入 URL で疎通しないようにする。
    """
    url = (base_url if base_url is not None else settings.platform_api_base_url or "").strip()
    if not url:
        raise InjectionError(
            "platform_api_base_url 未指定(引数か settings.platform_api_base_url を与えてください)"
        )
    if len(url) > MAX_BASE_URL_LEN:
        raise InjectionError(f"platform_api_base_url が長すぎます(>{MAX_BASE_URL_LEN})")
    if _VAULT_OCID_SUBSTR_RE.search(url):
        raise InjectionError("platform_api_base_url に Vault secret OCID。秘密を URL に置かない")
    if not _BASE_URL_RE.match(url):
        raise InjectionError(
            f"platform_api_base_url は https の URL で与えてください(平文 http 不可): {url}"
        )
    return url


def _closed_scopes(
    spec: ContainerDeploySpec, scopes: Iterable[str] | None
) -> tuple[str, ...]:
    """トークンへ載せる要求スコープを **配備仕様の必要スコープに閉じて** 決める(fail-closed)。

    既定(scopes=None)は配備仕様 `required_scopes` 全体。明示要求は **その部分集合のみ** 許可し、
    配備仕様が宣言していないスコープの要求(`scope_outside_spec`)・空要求は拒否する。承認グラントとの
    突き合わせ(承認超過拒否)は後段の `platform_grants.issue_token` が行う(二重閉包)。
    """
    declared = frozenset(spec.required_scopes)
    if not declared:
        # スコープを必要としないデモにトークンは要らない(注入経路は秘密を増やさない)。
        raise InjectionError(
            "配備仕様に required_scopes が無い(Platform 注入は不要)。スコープのある構成で呼ぶこと"
        )
    if scopes is None:
        want = declared
    else:
        want = frozenset(scopes)
        if not want:
            raise InjectionError("要求スコープが空(承認に閉じた最小スコープを与えること)")
        outside = want - declared
        if outside:
            raise InjectionError(
                f"配備仕様の宣言外スコープは要求できない: {sorted(outside)}"
                f"(required_scopes={sorted(declared)})"
            )
    return tuple(sorted(want))


def build_runtime_injection(
    spec: ContainerDeploySpec,
    *,
    tenant: str,
    plugin_id: str,
    settings: Settings | None = None,
    base_url: str | None = None,
    scopes: Iterable[str] | None = None,
    ttl_seconds: int | None = None,
) -> RuntimeInjection:
    """配備仕様＋テナント＋プラグインから、コンテナ起動時の Platform API 注入を組み立てる。

    手順(すべて fail-closed):
      1. ベース URL を解決・検証(https 固定・秘密混入拒否)。
      2. 要求スコープを **配備仕様 `required_scopes` に閉じて** 決める(宣言外/空は拒否)。
      3. `platform_grants.issue_token` で **承認グラントに厳密に閉じた** 短期 JWT を発行
         (グラント無し/失効/承認超過は GrantDenied で拒否=トークン未発行)。
      4. 発行直後に `platform_broker.verify_broker_token` で自己検証し、**実際に載ったスコープと
         失効時刻を権威値として確定**(発行と検証の乖離・未知スコープ混入を入口で検出)。

    返り値の `env()` は **非秘密のみ**(base_url)、`secret_env()` は **トークンのみ**(秘密)。
    DB 認証情報は読まない・注入しない(D5)。発行粒度は **呼び出しごと**(更新は再呼び出し)。
    """
    settings = settings or get_settings()

    resolved_url = _resolve_base_url(base_url, settings)
    want_scopes = _closed_scopes(spec, scopes)

    # 承認グラントに閉じた短期トークンを発行(承認超過は issue_token が scope_not_granted で拒否)。
    token = pg.issue_token(
        tenant,
        plugin_id,
        scopes=want_scopes,
        settings=settings,
        ttl_seconds=ttl_seconds,
    )

    # 発行直後の自己検証: 署名・iss/aud・必須クレーム・未知スコープ排除を通し、載ったスコープと
    # 失効時刻を権威値として取る(発行=検証の一致を入口で保証。fail-closed)。
    ctx = pb.verify_broker_token(token, settings=settings)
    issued_scopes = tuple(sorted(ctx.scopes))
    if issued_scopes != want_scopes:
        # 要求と発行が食い違うのは発行経路の不整合。安全側に倒す(注入しない)。
        raise InjectionError(
            f"発行トークンのスコープが要求と不一致: 発行={issued_scopes} 要求={want_scopes}"
        )

    injection = RuntimeInjection(
        base_url=resolved_url,
        token=token,
        scopes=issued_scopes,
        expires_at=ctx.expires_at,
        tenant=ctx.tenant,
        plugin_id=ctx.plugin_id,
    )

    # 多層防御の最終ゲート: 注入物のキーが allowlist 内で、非秘密側にトークン/Vault OCID が
    # 混じっていないこと(=DB 認証情報や秘密の運搬路になっていないこと)を構造的に確認する。
    _assert_injection_safe(injection)
    return injection


def _assert_injection_safe(injection: RuntimeInjection) -> None:
    """注入物の最終健全性チェック(多層防御)。キー allowlist・非秘密側の秘密混入なし・衝突なし。"""
    nonsecret = injection.env()
    secret = injection.secret_env()
    bad_nonsecret = set(nonsecret) - _ALLOWED_NONSECRET_KEYS
    if bad_nonsecret:
        raise InjectionError(f"非秘密 env に許可外キー: {sorted(bad_nonsecret)}")
    bad_secret = set(secret) - _ALLOWED_SECRET_KEYS
    if bad_secret:
        raise InjectionError(f"秘密 env に許可外キー: {sorted(bad_secret)}")
    if set(nonsecret) & set(secret):
        raise InjectionError("非秘密 env と秘密 env のキーが衝突している")
    # 非秘密側にトークンや Vault OCID が漏れていないこと(秘密の運搬路化を塞ぐ)。
    for key, value in nonsecret.items():
        if value == injection.token:
            raise InjectionError(f"非秘密 env '{key}' にトークンが漏れている")
        if _VAULT_OCID_SUBSTR_RE.search(value):
            raise InjectionError(f"非秘密 env '{key}' に Vault OCID")


def container_start_environment(
    spec: ContainerDeploySpec, injection: RuntimeInjection
) -> tuple[dict[str, str], dict[str, str]]:
    """コンテナ起動時の (非秘密 env, 秘密 env) を組み立てる(注入の適用点)。

    非秘密 env = 配備仕様の非秘密 env(deploy.py 生成)＋ 注入の base_url。
    秘密 env = 注入の短期トークンのみ。**DB 認証情報は含まない**。
    キー衝突(配備仕様が同名キーを既に持つ)は fail-closed で弾く(上書き事故防止)。
    """
    base = spec.module_environment()  # 非秘密のみ(deploy.py が保証)
    inj_nonsecret = injection.env()
    overlap = set(base) & set(inj_nonsecret)
    if overlap:
        raise InjectionError(f"配備仕様 env と注入 env のキー衝突: {sorted(overlap)}")
    nonsecret = dict(sorted({**base, **inj_nonsecret}.items()))
    secret = injection.secret_env()
    # 念のため: 非秘密側に秘密キーが無いこと(deploy.py の secret-hint ガードと整合)。
    if set(nonsecret) & set(secret):
        raise InjectionError("非秘密 env と秘密 env のキーが衝突している")
    return nonsecret, secret


def should_refresh(
    injection: RuntimeInjection,
    *,
    now: datetime | None = None,
    skew_seconds: int = 30,
) -> bool:
    """更新(再注入)すべきか。失効まで `skew_seconds` を切ったら True(ADR-0016 の更新方針)。

    長時間稼働するデモコンテナは、この判定で TTL 内に `build_runtime_injection` を再呼び出しして
    トークンを更新する。更新時に承認グラントが再評価されるため、失効は TTL 窓内で伝播する。
    """
    return injection.seconds_remaining(now) <= max(0, skew_seconds)


__all__ = [
    "PLATFORM_API_BASE_URL_ENV",
    "PLATFORM_TOKEN_ENV",
    "InjectionError",
    "RuntimeInjection",
    "build_runtime_injection",
    "container_start_environment",
    "should_refresh",
]
