"""生成デモへの Platform API ランタイム注入(L3 / ADR-0014 / ADR-0016。**DEP-03 で K8s Secret 化**)。

`deploy.py` は **秘密を一切持たない** 宣言的配備仕様(`ContainerDeploySpec`)を生成するところまでを担
う。
本モジュールは、その仕様を起点に **デモ Pod 起動時** のランタイム注入を組み立てる:

  - **ベース URL(非秘密)**: デモ Pod が Platform API へ到達する URL(`JETUSE_PLATFORM_API_BASE_URL`)
  。
    K8s では **ConfigMap**(`<prefix>-runtime`)に載せる(非秘密。committed/state に残ってよい)。
  - **短期トークン(秘密)**: `platform_grants.issue_token` が発行する **承認スコープに厳密に閉じた**
    短期 JWT(`JETUSE_PLATFORM_TOKEN`)。K8s では **Secret**(`<prefix>-platform-token`)に載せ、
    オーケストレータが **アウトオブバンドで `kubectl apply`**(Terraform/コミット/state を通さない)。
    **呼び出し(= Pod 起動/更新)ごとに発行**する(ADR-0014 §2 / platform_grants の発行粒度)。

**基盤の置換(ADR-0017)**: 配備ターゲットを Container Instances から OKE(K8s)へ移した。注入は
ConfigMap(base_url)＋ Secret(token)の **K8s マニフェスト描画**になり、Deployment は envFrom で両者を
参照する。**基盤非依存の核は不変**(下記)。

設計の核(ADR-0016。OKE でも保つ):
  - **DB 認証情報は注入しない**(D5)。デモ Pod はブローカー発行の短期トークンだけでテナントデータへ
    到達する。注入物は base_url(非秘密)＋ token(秘密)に限る。`adb_*` 等の DB 資格を読まない・載せな
    い。
  - **承認スコープに厳密に閉じる**: トークンに載るスコープは
    **配備仕様 `required_scopes`(デモが宣言した必要スコープ)∩ 承認グラント** に限定する。
    配備仕様が宣言していないスコープは要求できず(deploy-spec 閉包)、承認外スコープは
    `platform_grants.issue_token` が `scope_not_granted` で拒否(grant 閉包)。二重閉包・fail-closed。
  - **秘密と非秘密を分離**: `env()` は **非秘密のみ**(base_url)、トークンは `secret_env()` に分け、
    非秘密(ConfigMap)へ秘密を混ぜない。K8s でも ConfigMap=base_url / Secret=token に分けて描画する。

トークンのライフサイクル(ADR-0016。OKE ネイティブ):
  - **TTL は短期**(`settings.platform_token_ttl_seconds`、broker 上限 900 秒)。`expires_at` で失効時
  刻を公開。
  - **失効(revoke)**: `platform_grants.revoke_grant` 後の再発行(=次回起動/更新)は `grant_revoked` で
    拒否される。即時失効機構(jti 失効リスト)は MVP 非対象のため、**失効の有効化窓 = TTL**。fail-clos
    ed。
  - **更新(refresh)**: 短期 TTL のため、長時間稼働するデモ Pod は TTL 内に **再注入(= 本関数の再呼び
  出し
    → Secret を新値で再 apply → Deployment を rolling restart)で更新** する(ADR-0017 §6)。更新時に承
    認
    グラントが再評価されるため、失効は TTL 窓内で伝播する。env 固定だった CI の制約は OKE で解消する
    。

セキュリティ姿勢: **fail-closed**(deploy.py / platform_broker.py と同方針)。base_url 未設定/不正、
スコープが配備仕様の閉包外/空、グラント無し/失効/承認超過、トークンへの Vault OCID 混入のいずれでも
注入を組み立てない。**短期トークンは ConfigMap・committed マニフェスト・IaC state に出さない**(Secre
t のみ)。
"""

from __future__ import annotations

import base64
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from . import platform_broker as pb
from . import platform_grants as pg
from .deploy import _VAULT_OCID_SUBSTR_RE, ContainerDeploySpec, tenant_hash_hex
from .settings import Settings, get_settings

# 注入する env キー(契約: deploy.py の Deployment envFrom と一致させる)。
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

        **Terraform/コミット/IaC state には残さない**(渡した値は resource 入力として state に保存さ
        れ、
        短期トークンを永続化してしまうため)。トークンは起動時の **アウトオブバンド注入** — K8s では
        オーケストレータが `<prefix>-platform-token` Secret を `kubectl apply` し、Deployment が env
        From で
        参照する(ADR-0017 §5)。短期 TTL で揮発し、更新は `should_refresh` 判定での再注入(Secret 再 a
        pply
        ＋ rolling restart)で行う。
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

    # ---- K8s(OKE)注入マニフェスト描画(ADR-0017 §5)。名前は spec の命名規約に一致させる ----

    def _assert_spec_tenant(self, spec: ContainerDeploySpec) -> None:
        """描画/起動 env が **この注入と同じ spec(テナント＋発行プラグイン)** に対してのみ行える
        ことを保証する。

        - spec が tenant ハッシュ付きなら `tenant_hash_hex(self.tenant)` と一致必須(F-001)。
        - spec が plugin_id 固定なら `self.plugin_id` と一致必須。無いと plugin B で発行した注入を
          plugin A の spec に渡して描画でき、別プラグインのグラントへすり替えられる。
        いずれも不一致は fail-closed(他テナント/他プラグインの namespace/Secret 名で出力させない)。
        """
        if spec.tenant_hash and tenant_hash_hex(self.tenant) != spec.tenant_hash:
            raise InjectionError(
                "注入の発行テナントと spec のテナントが不一致(別テナントへの描画/起動を拒否)"
            )
        if spec.plugin_id and self.plugin_id != spec.plugin_id:
            raise InjectionError(
                "注入の発行プラグインと spec のプラグインが不一致(別プラグインへの描画/起動を拒否)"
            )

    def render_runtime_configmap(self, spec: ContainerDeploySpec) -> dict:
        """**非秘密**(base_url)を載せる ConfigMap(`<prefix>-runtime`)を描画する。

        非秘密なので committed/state に残ってよい。トークンは決して載せない(`env()` を使う=allowlist
         経由)。
        """
        self._assert_spec_tenant(spec)
        return {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": spec.runtime_config_map_name,
                "namespace": spec.namespace,
                "labels": spec.labels(),
            },
            "data": dict(sorted(self.env().items())),
        }

    def render_secret_manifest(self, spec: ContainerDeploySpec) -> dict:
        """**秘密**(短期トークン)を載せる K8s Secret(`<prefix>-platform-token`)を描画する。

        オーケストレータが **アウトオブバンドで `kubectl apply --server-side`** する
        (Terraform/コミット/state を通さない=ADR-0017 §5)。トークンは **base64 済み `data`**
        (`{JETUSE_PLATFORM_TOKEN: <base64>}`、`secret_env()` 経由)。`stringData` は使わない
        (server-side apply の field 管理が不安定で refresh の in-place 更新が不確実なため。下の実装
        コメント参照)。失効時刻を annotation で公開し(非秘密)、refresh ツールが TTL を読めるように
        する。トークン値(平文/base64 とも)は annotation/label に出さない。
        """
        self._assert_spec_tenant(spec)
        return {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": spec.token_secret_name,
                "namespace": spec.namespace,
                "labels": spec.labels(),
                "annotations": {
                    # 非秘密メタ(失効時刻・スコープ)。トークン本体は載せない。
                    "jetuse.dev/token-expires-at": self.expires_at.isoformat(),
                    "jetuse.dev/token-scopes": ",".join(self.scopes),
                },
            },
            "type": "Opaque",
            # `stringData` ではなく base64 済み `data` を出す: server-side apply は stringData
            # (書込専用変換フィールド)の field 管理が不安定で、refresh の in-place 更新が反映され
            # ないことがある。`data` なら apply 経路に依らず決定的に更新される(review 対応)。
            "data": {k: base64.b64encode(v.encode("utf-8")).decode("ascii")
                     for k, v in sorted(self.secret_env().items())},
        }

    def render_injection_manifests(self, spec: ContainerDeploySpec) -> list[dict]:
        """注入の K8s マニフェスト群を描画する。

        [ConfigMap(base_url=非秘密), Secret(token=秘密)] のリストを返す。
        """
        return [self.render_runtime_configmap(spec), self.render_secret_manifest(spec)]


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


def _closed_scopes(spec: ContainerDeploySpec, scopes: Iterable[str] | None) -> tuple[str, ...]:
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

    # tenant は **非空必須**(空/空白は環境変数展開ミスの兆候。トークン発行前に fail-closed)。
    if not tenant or not tenant.strip():
        raise InjectionError("tenant が空(空白のみ)。有効なテナント識別子を渡すこと")
    # 前後空白を正規化(build_deploy_spec と同じ規約)。spec の tenant_hash・grant 発行と一致させる。
    tenant = tenant.strip()

    # テナント分離: spec が tenant ハッシュ付き(マルチテナント配備)なら、注入する tenant が
    # **その spec のテナントと一致** することを fail-closed で要求する。さもないと tenant B の
    # トークンを tenant A の namespace/Secret(spec 由来)へ注入でき、分離が破れる(review F-001)。
    if spec.tenant_hash and tenant_hash_hex(tenant) != spec.tenant_hash:
        raise InjectionError(
            "tenant が spec のテナントと不一致(別テナントの Secret/namespace への注入を拒否)"
        )

    # 発行プラグインの binding(core でも強制。CLI live-check だけに依存しない多層防御)。
    plugin_id = plugin_id.strip()
    if not plugin_id:
        raise InjectionError("plugin_id が空。発行主体プラグインを指定すること")
    # 注入が必要(scoped)なのに spec が plugin 未固定だと ground truth が無く別プラグインへすり替え
    # 可能なため発行しない。固定済みなら一致必須(別プラグインのグラントへのすり替えを core で拒否)。
    if spec.needs_platform_injection and not spec.plugin_id:
        raise InjectionError(
            "injectable な spec は plugin_id を固定して deploy すること(plugin binding)"
        )
    if spec.plugin_id and plugin_id != spec.plugin_id:
        raise InjectionError(
            "plugin_id が spec の発行プラグインと不一致(別プラグインへのすり替えを拒否)"
        )

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
    """Pod 起動時に **実際に効く** (非秘密 env, 秘密 env) を組み立てる(注入の論理適用点)。

    K8s では非秘密 env は ConfigMap(静的＝`<prefix>-config` ＋ 注入＝`<prefix>-runtime`)、
    秘密 env は Secret(`<prefix>-platform-token`)に分かれ、Deployment の envFrom で
    **K8s が実行時にマージ**する。本関数はその **マージ結果**(Pod が見る最終 env)を返し、
    キー衝突や秘密の非秘密側への漏れを fail-closed で検出する(マニフェスト描画とは別に、
    注入契約の不変条件を 1 か所で検証する)。

    非秘密 env = 配備仕様の非秘密 env(deploy.py 生成)＋ 注入の base_url。
    秘密 env = 注入の短期トークンのみ。**DB 認証情報は含まない**。
    キー衝突(配備仕様が同名キーを既に持つ)は fail-closed で弾く(上書き事故防止)。
    """
    # テナント分離: 注入の発行テナントと spec のテナントが一致することを fail-closed で要求する
    # (別テナント spec への起動 env 組み立てを拒否。render_* と同じ不変条件。F-001)。
    injection._assert_spec_tenant(spec)
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
