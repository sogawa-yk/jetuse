"""生成デモの L3 配備仕様生成（DEP-01 起票 / **DEP-03 で OKE(Kubernetes)へ置換**）。

合成済み・ガバナンス ok の `DemoComposition`(synth.py)から、**Kubernetes(OKE)ワークロード**として
そのまま `kubectl apply` できる **宣言的なデプロイ・マニフェスト**(Namespace / ConfigMap / Deploymen
t /
Service ほか)を **決定的に** 生成する。新規インフラ(クラスタ/VCN/LB/IAM)のプロビジョニングはしない
(D8: デモはアプリ層成果物=namespace＋マニフェストに閉じる)。固定基盤(OKE クラスタ・ADR-0017)の保証が
効く。

**基盤の置換(ADR-0017)**: DEP-01 は配備ターゲットを Container Instances(tfvars)としていたが、L3 実行
基盤を
OKE へ移行した。配備ターゲットは **K8s マニフェスト**になり、デモは 1 namespace = 1 デモとして deplo
y
(`kubectl apply`)/delete(`kubectl delete namespace`)が trivial になる。**基盤非依存の核は不変**:
秘密を持たない宣言的配備仕様 / 非秘密 env のみ / required_secrets は allowlist / ブローカー一本のデ
ータ注入。

再利用(新規の実行基盤・認可経路は作らない):
  - ADR-0009: SDK→ホスト型 Application OCID 解決
    (`hosted_agent.normalize_sdk` / 設定 `agent_*_app_ocid`)。
  - ADR-0011: 配備イメージは OCIR(ap-osaka-1)。worker からの pull もここに集約。
  - Platform API ブローカー(ADR-0014/D5): デモ Pod は **DB 資格情報を持たず**、ブローカー発行の
    スコープ付き短期トークンでテナントデータへ到達する。配備仕様には付与予定の `required_scopes`
    のみを記録する(実トークンは持たせない。実注入は `deploy_inject` = DEP-02/03 の K8s Secret 注入)
    。

セキュリティ姿勢: **fail-closed** かつ **秘密(実値も OCID 参照も)をマニフェスト/IaC state に残さない
**。
  - composition.ok でない/ガバナンス未通過の構成は配備仕様を作らない(DeploySpecError)。
  - 秘密は **「要求する秘密の論理名」だけを宣言**する(`required_secrets`、allowlist 制約)。
    具体的な Vault secret OCID の解決と注入は **`deploy_inject`(Platform API 注入)** の責務であり、
    本仕様は
    Vault OCID を一切持たない・マニフェストにも出さない(K8s ConfigMap=非秘密のみ。Secret 注入は別経
    路)。
  - 非秘密 env(`environment_variables` → ConfigMap)は **非秘密のみ**(キーは OCI_REGION/JETUSE_*)。
    短期トークンは ConfigMap に載せず、`deploy_inject` が生成する K8s Secret として注入する。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

import yaml

from .hosted_agent import normalize_sdk
from .settings import Settings, get_settings

if TYPE_CHECKING:  # 循環 import 回避(synth は本モジュールに依存しない)
    from .synth import DemoComposition

# Vault secret OCID の断片(値のどこかに混入していても弾くため、アンカーなしの部分一致で探す)。
# 完全一致だと値に埋め込まれた OCID を見逃すため、search(部分一致)で判定する。
_VAULT_OCID_SUBSTR_RE = re.compile(r"ocid1\.vaultsecret\.")
# OCIR イメージの形。ADR-0011 は ap-osaka-1(`kix.ocir.io`)public を前提とするため、レジストリ
# ホストを kix.ocir.io に固定する(phx 等の別リージョン OCIR や非 OCIR を弾く)。形は
# `kix.ocir.io/<namespace>/<repo>[:tag]`。namespace はテナンシ固有。
# 空セグメント(`ns//repo`)や末尾スラッシュ(`ns/repo/`)を弾くため、各パスセグメントを非空に固定。
_OCIR_IMAGE_RE = re.compile(r"^kix\.ocir\.io/[^/\s:]+(?:/[^/\s:]+)+(?::[^\s:]+)?$")
# env 名の形(POSIX 風。コンテナ env として安全な英大文字・数字・アンダースコア)。
_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
# 追加(extra)env キーの名前空間。デモ固有の非秘密メタはこの接頭辞に限定する(allowlist)。
# 生成済み予約キーは OCI_REGION ＋ JETUSE_*。ゆえにコンテナ env キーは常に OCI_REGION か JETUSE_*。
_EXTRA_ENV_PREFIX = "JETUSE_"
# 秘密らしい/資格情報を運びがちなキー名。非秘密 env(extra_environment)では弾き、要求秘密は
# `required_secrets`(論理名の宣言)へ誘導する。DB 資格情報(D5: L3 は DB 直結しない)も広めに弾く。
# TF 側 variables.tf の environment_variables validation と同じパターンに保つ。
_SECRET_KEY_HINT_RE = re.compile(
    r"SECRET|PASS|PWD|TOKEN|CREDENTIAL|PRIVATE|KEY|AUTH|CERT|SIGNATURE"
    r"|DSN|DATABASE_URL|CONNECTION_STRING",
    re.IGNORECASE,
)

# 配備するデモコンテナの既定リソース(D8: 上限=コンテナ。最小構成)。
# 既定 8000 は **本リポジトリのデモ/本体イメージの契約**(Containerfile `EXPOSE 8000`・hosted-demo
# terraform 既定も 8000)。別ポートで listen するイメージは `build_deploy_spec(app_port=...)` で
# 上書きする(Service targetPort とコンテナ port が連動して変わる)。
DEFAULT_APP_PORT = 8000
DEFAULT_OCPUS = 1.0
DEFAULT_MEMORY_GB = 8.0
# OCI Container Instance(CI.Standard.E4.Flex)の妥当域。範囲外/端数は plan/apply 前に弾く。
MAX_PORT = 65535
MIN_OCPUS = 1
MAX_OCPUS = 64
MIN_MEMORY_GB = 1
MAX_MEMORY_GB = 1024

# 配備先リージョン(ADR-0011: ap-osaka-1 固定。OCIR=kix・jetuse-dev も ap-osaka)。
DEPLOY_REGION = "ap-osaka-1"

# 上限(暴走・巨大マニフェスト防止。store/scaffold と同方針)。
MAX_ENV_VARS = 100
MAX_REQUIRED_SECRETS = 50
# K8s namespace/label/Deployment 名は DNS-1123 ラベル(<=63 文字)。prefix を namespace 兼デプロイ名に
# 使うため、その制約内に収める(従来の 40 でも十分。明示しておく)。
MAX_PREFIX_LEN = 40

# テナント非秘密ハッシュの 16 進長(= sha256 先頭 N 文字)。32bit(8)では大規模で衝突しうるため
# 48bit(12)へ。namespace/Secret 命名と _assert_spec_tenant の一致判定の両方がこの 1 か所を使う。
TENANT_HASH_LEN = 12


def tenant_hash_hex(tenant: str) -> str:
    """テナントの非秘密ハッシュ(sha256 先頭 TENANT_HASH_LEN hex)。生 OCID は出力に残さない。"""
    return hashlib.sha256(tenant.encode("utf-8")).hexdigest()[:TENANT_HASH_LEN]

# K8s マニフェストのメタ(ADR-0017)。デモは 1 namespace = 1 デモ。名前は prefix から決定的に導く。
# Deployment/Service/ConfigMap/Secret/ServiceAccount 名は prefix を基点に固定し、deploy_inject が
# 生成する Secret/ConfigMap 名(token/base_url)もこの規約に一致させる(両者で同じ名前を参照する)。
K8S_CONTAINER_NAME = "api"
#: 非秘密の静的 env を載せる ConfigMap 名サフィックス。
CONFIG_MAP_SUFFIX = "-config"
#: 注入時の非秘密(base_url)を載せる ConfigMap 名サフィックス(deploy_inject が apply)。
RUNTIME_CONFIG_MAP_SUFFIX = "-runtime"
#: 注入時の秘密(短期トークン)を載せる Secret 名サフィックス(deploy_inject が apply)。
TOKEN_SECRET_SUFFIX = "-platform-token"
#: ServiceAccount 名サフィックス(最小権限。トークン自動マウントは無効にする)。
SERVICE_ACCOUNT_SUFFIX = "-sa"

# Service の公開ポート(ClusterIP。内部到達。外部公開は本体 Ingress/LB 側で別途)。
DEFAULT_SERVICE_PORT = 80
#: デモ Pod のレプリカ数(D8: 最小)。
DEFAULT_REPLICAS = 1
#: デモ Pod を実行する非 root UID/GID(runAsUser/runAsGroup)。
#: これにより runAsNonRoot を kubelet が検証でき、Pod 拒否を防ぐ。
NONROOT_UID = 10001

# build_deploy_spec が必ず生成する予約 env キー。extra_environment での上書きを禁じる。
# **注入経路が所有するキー**(deploy_inject の base_url / token)も予約に含める: 静的 ConfigMap
# (extra_environment 経由)へ入れられると、Deployment が runtime ConfigMap / Secret と
# 同名キーを envFrom し、K8s 実行時にキー衝突・上書きが起きて注入の fail-closed 検査を迂回しうる。
# (循環 import 回避のため deploy_inject の定数名を文字列で持つ。両モジュールで一致させること。)
_RESERVED_ENV_KEYS = frozenset(
    {
        "OCI_REGION",
        "JETUSE_SDK",
        "JETUSE_DEMO_APP",
        "JETUSE_ACTIVE_CONNECTORS",
        "JETUSE_PLATFORM_SCOPES",
        "JETUSE_AGENT_APP_OCID",
        "JETUSE_PLATFORM_API_BASE_URL",  # = deploy_inject.PLATFORM_API_BASE_URL_ENV(注入経路所有)
        "JETUSE_PLATFORM_TOKEN",         # = deploy_inject.PLATFORM_TOKEN_ENV(注入経路所有・秘密)
    }
)

# L3 デモコンテナが**要求してよい秘密(論理名)の許可リスト**(ADR-0014/D5: 越境・最小権限)。
# デモコンテナは DB 資格情報も broker 署名鍵も持たず、テナントデータへは Platform 発行の
# 短期トークンでのみ到達する。コンテナ自身が短期トークンを取得するための OIDC クライアント資格だけが
# 正当な要求秘密。それ以外(DB_PASSWORD / ADB_WALLET_PASSWORD / 他)
# は **すべて fail-closed で拒否**(denylist でなく allowlist)。追加はレビュー要。
# **本仕様は秘密の論理名のみ宣言**。Vault OCID 解決・注入は DEP-02(OCID は持たない)。
_ALLOWED_SECRET_NAMES = frozenset(
    {
        "HOSTED_AGENT_CLIENT_SECRET",  # コンテナの OIDC クライアント秘密(短期トークン取得用)
    }
)

# 名指しで分かりやすいエラーを返す「持たせてはいけない代表的な秘密」(allowlist 不通過の補助説明)。
_FORBIDDEN_SECRET_HINTS = {
    "PLATFORM_BROKER_SECRET": "broker 署名鍵(任意トークン偽造の危険)",
    "DB_PASSWORD": "DB 資格情報(L3 は DB 直結しない=ブローカー経由)",
    "ADB_WALLET_PASSWORD": "DB ウォレット資格(同上)",
    "DATABASE_URL": "DB 接続情報(同上)",
}

# SDK→ホスト型 Application OCID を引く設定属性(ADR-0009。hosted_agent._SDK_ATTR と一致)。
_SDK_APP_OCID_ATTR = {
    "openai_agents": "agent_openai_app_ocid",
    "langgraph": "agent_langgraph_app_ocid",
    "adk": "agent_adk_app_ocid",
}


class DeploySpecError(ValueError):
    """配備仕様を生成できない(fail-closed)。未合成/ガバナンス未通過/不正な秘密要求等。"""


@dataclass(frozen=True)
class ContainerDeploySpec:
    """L3 デモの宣言的配備仕様。**K8s(OKE)マニフェスト**(Namespace/ConfigMap/Deployment/Service)へ
    決定的に写像できる(ADR-0017)。

    `environment_variables` は **非秘密のみ**(キーは OCI_REGION か JETUSE_*)。秘密は値も OCID 参照も
    本仕様・マニフェスト・IaC state に持たない。代わりに `required_secrets`(論理名)だけを残し、
    具体的な Vault OCID 解決・コンテナ注入は **`deploy_inject`(Platform API 注入 / K8s Secret)** が
    担う。
    """

    #: 配備先リージョン(ADR-0011: ap-osaka-1 固定)。env(OCI_REGION)に明示してリージョンを固定する。
    region: str
    #: K8s namespace/Deployment/Service/ConfigMap 名の基点(`<prefix>` を namespace に使う。DNS-1123)
    # 。
    prefix: str
    #: OCIR(ap-osaka-1)イメージ URL(ADR-0011)。
    image_url: str
    #: コンテナ待受ポート(API GW 連携の既定 8000)。
    app_port: int
    ocpus: float
    memory_gb: float
    #: ADR-0009 で正規化した SDK(openai_agents|langgraph|adk)。
    sdk: str
    #: 解決できたホスト型 Application OCID(設定に無ければ None)。
    agent_app_ocid: str | None
    #: 非秘密の環境変数(決定的・ソート済みで生成。キーは OCI_REGION か JETUSE_*)。
    #: frozen 仕様の不変性を保つため読み取り専用 Mapping(MappingProxyType)で保持する。
    environment_variables: Mapping[str, str]
    #: コンテナが要求する秘密の**論理名**(allowlist 制約・ソート済み)。**Vault OCID は持たない**。
    #: 具体的な解決と注入は deploy_inject。本仕様/マニフェスト/state には機微な参照先を残さない。
    required_secrets: tuple[str, ...]
    #: ブローカーから付与予定の Platform スコープ(active コネクタ由来の和集合・ソート済み)。
    required_scopes: tuple[str, ...]
    #: invoke 経路へ載る active コネクタ provider(記録用)。
    active_connectors: tuple[str, ...]
    #: デモ識別子(sample_app。K8s label `jetuse.dev/demo` の値。健全化済み・既定空)。
    sample_app: str = ""
    #: テナント非秘密ハッシュ(tenant 指定時。namespace 一意化に prefix へ含め、label にも持たせる)。
    tenant_hash: str = ""
    #: 発行主体プラグイン ID(承認グラントのキー)。deploy 時に Deployment 注釈へ固定し、注入が別
    #: プラグインのグラントへすり替えるのを防ぐ ground truth(指定時のみ注釈に出す)。
    plugin_id: str = ""

    def __post_init__(self) -> None:
        # frozen dataclass の不変性を環境変数 map にも及ぼす(構築後の差し込みを防ぐ)。
        object.__setattr__(
            self, "environment_variables", MappingProxyType(dict(self.environment_variables))
        )

    def module_environment(self) -> dict[str, str]:
        """デモ Pod へ渡す環境変数(**非秘密のみ**・決定的)。

        秘密はここに合流させない(短期トークンは K8s Secret 経由=deploy_inject)。
        """
        return dict(sorted(self.environment_variables.items()))

    # ---- K8s(OKE)命名(ADR-0017。deploy_inject も同じ名前で Secret/ConfigMap を参照する) ----

    @property
    def namespace(self) -> str:
        """デモ専用 namespace(= prefix。1 namespace = 1 デモ。DNS-1123 ラベル)。"""
        return self.prefix

    @property
    def config_map_name(self) -> str:
        """非秘密の静的 env を載せる ConfigMap 名。"""
        return f"{self.prefix}{CONFIG_MAP_SUFFIX}"

    @property
    def runtime_config_map_name(self) -> str:
        """注入時の非秘密(base_url)を載せる ConfigMap 名(deploy_inject が apply)。"""
        return f"{self.prefix}{RUNTIME_CONFIG_MAP_SUFFIX}"

    @property
    def token_secret_name(self) -> str:
        """注入時の秘密(短期トークン)を載せる Secret 名(deploy_inject が apply)。"""
        return f"{self.prefix}{TOKEN_SECRET_SUFFIX}"

    @property
    def service_account_name(self) -> str:
        """デモ Pod の ServiceAccount 名(最小権限。トークン自動マウントは無効)。"""
        return f"{self.prefix}{SERVICE_ACCOUNT_SUFFIX}"

    @property
    def needs_platform_injection(self) -> bool:
        """Platform API 注入(base_url＋短期トークン)を要するか(= スコープ宣言があるか)。

        required_scopes が空のデモはトークンを必要としない(deploy_inject も拒否する)。その場合は
        Deployment に注入用 ConfigMap/Secret を envFrom しない(存在しない Secret 参照で Pod が
        起動不能になるのを防ぐ。fail-closed と起動性の両立)。
        """
        return bool(self.required_scopes)

    def labels(self) -> dict[str, str]:
        """全リソース共通のラベル(棚卸し・選択の決定的キー)。

        ADR-0016 §6 のタグ規約を K8s ラベルへ写像する。
        """
        labels = {
            "app.kubernetes.io/name": self.prefix,
            "app.kubernetes.io/managed-by": "jetuse-deploy",
            "jetuse.dev/kind": "hosted-demo",
        }
        demo = _sanitize_label_value(self.sample_app)
        if demo:
            labels["jetuse.dev/demo"] = demo
        if self.tenant_hash:
            # テナントは非秘密ハッシュで持つ(OCID 全長は label 上限 63 超。棚卸し/分離キー)。
            labels["jetuse.dev/tenant"] = self.tenant_hash
        return dict(sorted(labels.items()))

    def selector_labels(self) -> dict[str, str]:
        """Deployment/Service の selector(不変な最小キー。label の一部)。"""
        return {
            "app.kubernetes.io/name": self.prefix,
            "jetuse.dev/kind": "hosted-demo",
        }

    def _env_from(self) -> list[dict]:
        """Deployment コンテナの envFrom(静的 ConfigMap ＋ 注入 ConfigMap/Secret)。

        注入(base_url/token)は **スコープのあるデモのみ**参照する。注入物は deploy_inject が
        アウトオブバンドで apply する(短期 TTL・state/コミットに残さない=ADR-0017 §5)。
        参照は `optional: false`(存在必須)で、トークン未注入のまま起動しない fail-closed。
        """
        sources: list[dict] = [
            {"configMapRef": {"name": self.config_map_name}},
        ]
        if self.needs_platform_injection:
            sources.append(
                {"configMapRef": {"name": self.runtime_config_map_name, "optional": False}}
            )
            sources.append({"secretRef": {"name": self.token_secret_name, "optional": False}})
        return sources

    def render_manifests(self) -> list[dict]:
        """`kubectl apply -f` できる K8s マニフェスト(辞書のリスト)を決定的に生成する。

        含むもの: Namespace / ResourceQuota / ServiceAccount / ConfigMap(**非秘密 env のみ**) /
        Deployment / Service。**短期トークンの Secret はここに含めない**(deploy_inject が別経路で ap
        ply
        する。マニフェスト/コミット/state に秘密を残さない=ADR-0017 §5)。
        """
        labels = self.labels()
        meta_ns = {"name": self.namespace, "labels": dict(labels)}

        namespace = {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": meta_ns,
        }

        # namespace 単位の上限(越境・暴走抑止。ADR-0017 §4)。D8: 最小 1 Pod。
        resource_quota = {
            "apiVersion": "v1",
            "kind": "ResourceQuota",
            "metadata": {
                "name": f"{self.prefix}-quota",
                "namespace": self.namespace,
                "labels": dict(labels),
            },
            "spec": {
                "hard": {
                    "pods": "4",
                    "requests.cpu": str(self.ocpus * 2),
                    "requests.memory": f"{int(self.memory_gb) * 2}Gi",
                    "limits.cpu": str(self.ocpus * 2),
                    "limits.memory": f"{int(self.memory_gb) * 2}Gi",
                }
            },
        }

        service_account = {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {
                "name": self.service_account_name,
                "namespace": self.namespace,
                "labels": dict(labels),
            },
            # K8s API トークンを Pod に自動マウントしない(デモは broker 経由でデータ到達。最小権限)
            # 。
            "automountServiceAccountToken": False,
        }

        config_map = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": self.config_map_name,
                "namespace": self.namespace,
                "labels": dict(labels),
            },
            # 非秘密のみ(deploy.py が保証)。短期トークン/Vault OCID は載らない。
            "data": self.module_environment(),
        }

        container = {
            "name": K8S_CONTAINER_NAME,
            "image": self.image_url,
            "ports": [{"containerPort": self.app_port}],
            "envFrom": self._env_from(),
            "resources": {
                "requests": {"cpu": str(self.ocpus), "memory": f"{int(self.memory_gb)}Gi"},
                "limits": {"cpu": str(self.ocpus), "memory": f"{int(self.memory_gb)}Gi"},
            },
            # コンテナレベルの最小権限(非 root・権限昇格不可・全 capability drop・seccomp 既定)。
            # **runAsUser/runAsGroup を明示**して非 root を強制する(イメージの USER が root 既定や
            # 未指定でも kubelet が runAsNonRoot を検証でき Pod 拒否を防ぐ)。
            # 配備イメージは任意 UID で動く契約。
            "securityContext": {
                "allowPrivilegeEscalation": False,
                "runAsNonRoot": True,
                "runAsUser": NONROOT_UID,
                "runAsGroup": NONROOT_UID,
                "capabilities": {"drop": ["ALL"]},
                "seccompProfile": {"type": "RuntimeDefault"},
            },
        }

        deployment = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": self.prefix,
                "namespace": self.namespace,
                "labels": dict(labels),
                # 注入トークンが従う **配備仕様の宣言スコープ**(deploy-spec 閉包)を監査用に出す。
                # 注入はこの宣言に閉じ、承認グラントとの二重閉包で最終決定。plugin-id を固定し、
                # 注入が別プラグインのグラントへすり替えるのを ground truth で防ぐ(指定時のみ)。
                "annotations": {
                    "jetuse.dev/required-scopes": ",".join(self.required_scopes),
                    **({"jetuse.dev/plugin-id": self.plugin_id} if self.plugin_id else {}),
                },
            },
            "spec": {
                "replicas": DEFAULT_REPLICAS,
                "selector": {"matchLabels": self.selector_labels()},
                "template": {
                    "metadata": {"labels": dict(labels)},
                    "spec": {
                        "serviceAccountName": self.service_account_name,
                        "automountServiceAccountToken": False,
                        "containers": [container],
                    },
                },
            },
        }

        service = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": self.prefix, "namespace": self.namespace, "labels": dict(labels)},
            "spec": {
                "type": "ClusterIP",
                "selector": self.selector_labels(),
                "ports": [
                    {"port": DEFAULT_SERVICE_PORT, "targetPort": self.app_port, "protocol": "TCP"}
                ],
            },
        }

        return [namespace, resource_quota, service_account, config_map, deployment, service]

    def render_manifests_yaml(self) -> str:
        """`kubectl apply -f` 用の決定的 YAML(複数ドキュメント)。秘密値・Vault OCID なし。"""
        return dump_manifests(self.render_manifests())

    # ---- 後方互換: Container Instances tfvars 経路(stage-4 ベースライン維持) ----
    # ADR-0017 で L3 の **新規** 配備ターゲットは K8s(render_manifests)に移行したが、stage-4 の
    # Container Instances 基盤(environments/hosted-demo)は **ベースラインとして残置** するため、
    # 既存の tfvars 写像 API も後方互換でそのまま維持する(公開シグネチャ不変)。

    def to_tfvars(self) -> dict:
        """hosted-demo Container Instances 環境(environments/hosted-demo)の変数へ写像した dict。

        stage-4 ベースライン(CI 経路)の後方互換。K8s 経路は `render_manifests()` を使う。
        コンパートメント/サブネット/NSG は基盤側が供給する infra 値で含めない。**秘密(Vault OCID)は
        含めない**(`required_secrets` は仕様メタで tfvars/state には出さない)。
        """
        return {
            "region": self.region,
            "prefix": self.prefix,
            "image_url": self.image_url,
            "app_port": self.app_port,
            "ocpus": self.ocpus,
            "memory_gb": self.memory_gb,
            "environment_variables": dict(self.environment_variables),
        }

    def render_tfvars_json(self) -> str:
        """`*.auto.tfvars.json` 用の決定的 JSON(キーソート)。秘密値なし(CI 経路の後方互換)。"""
        return json.dumps(self.to_tfvars(), ensure_ascii=False, indent=2, sort_keys=True)


def resolve_agent_app_ocid(sdk: str, settings: Settings) -> str | None:
    """ADR-0009: 正規化済み SDK からホスト型 Application OCID を引く(未設定は None)。"""
    attr = _SDK_APP_OCID_ATTR.get(sdk)
    if not attr:
        return None
    return getattr(settings, attr, "") or None


def _validate_required_secrets(required_secrets: Iterable[str] | None) -> tuple[str, ...]:
    """要求秘密の**論理名**を検証(allowlist・名前形式・予約キー保護)。Vault OCID は扱わない。

    fail-closed: allowlist 外の名(DB 資格情報・broker 署名鍵 等)・不正名・予約キーは拒否する。
    """
    if not required_secrets:
        return ()
    names = list(required_secrets)
    if len(names) > MAX_REQUIRED_SECRETS:
        raise DeploySpecError(f"required_secrets が多すぎます(>{MAX_REQUIRED_SECRETS})")
    out: set[str] = set()
    for name in names:
        if not isinstance(name, str) or not _ENV_NAME_RE.match(name):
            raise DeploySpecError(
                f"required secret 名 '{name}' が不正です(英大文字/数字/アンダースコアのみ)"
            )
        if name in _RESERVED_ENV_KEYS:
            raise DeploySpecError(f"予約 env キー '{name}' は required_secrets に使えません")
        if name not in _ALLOWED_SECRET_NAMES:
            # ADR-0014/D5: L3 は許可リスト外の秘密(DB 資格情報・broker 署名鍵 等)を要求しない。
            hint = _FORBIDDEN_SECRET_HINTS.get(name, "L3 が保持してよい秘密ではない")
            raise DeploySpecError(
                f"'{name}' は L3 コンテナへ注入禁止({hint})。"
                f"許可されるのは {sorted(_ALLOWED_SECRET_NAMES)} のみ(ADR-0014/D5)"
            )
        out.add(name)
    return tuple(sorted(out))


def _collect_required_scopes(composition: DemoComposition) -> tuple[str, ...]:
    """active コネクタ束縛の required_scopes を和集合(ソート済み)で集める(D5: 付与予定スコープ)。"""
    scopes: set[str] = set()
    for binding in composition.connector_bindings:
        if binding.status == "active":
            scopes.update(binding.required_scopes)
    return tuple(sorted(scopes))


def _validate_extra_environment(
    extra_environment: Mapping[str, str] | None,
) -> dict[str, str]:
    """呼び出し側の追加 env を検証する(非秘密のみ・名前空間 allowlist・名前形式・秘密混入拒否)。

    fail-closed: 追加 env キーは **`JETUSE_` 名前空間に限定**する(allowlist)。これにより
    DB_PASSWORD/DB_PASS/OPENAI_KEY/SLACK_TOKEN のような任意の資格情報キーで秘密実値を非秘密 env に
    紛れ込ませる経路を **構造的に** 閉じる(秘密は required_secrets 宣言経由・DEP-02)。
    予約キー上書き・不正名・秘密らしいキー名・Vault secret OCID らしき値も併せて拒否(多層)。
    """
    if not extra_environment:
        return {}
    out: dict[str, str] = {}
    for key, value in extra_environment.items():
        if not isinstance(value, str):
            raise DeploySpecError(f"environment '{key}' の値は文字列で与えてください")
        if not _ENV_NAME_RE.match(key):
            raise DeploySpecError(
                f"environment 名 '{key}' が不正です(英大文字/数字/アンダースコアのみ)"
            )
        if key in _RESERVED_ENV_KEYS:
            raise DeploySpecError(f"予約 env キー '{key}' は extra_environment で上書きできません")
        if not key.startswith(_EXTRA_ENV_PREFIX):
            # 名前空間 allowlist: 追加 env は JETUSE_ 接頭辞のみ。任意キー(資格情報名含む)を排除。
            raise DeploySpecError(
                f"追加 env '{key}' は '{_EXTRA_ENV_PREFIX}' 接頭辞のみ(秘密は required_secrets)"
            )
        if _SECRET_KEY_HINT_RE.search(key):
            raise DeploySpecError(
                f"秘密らしい env '{key}' は不可。required_secrets で論理名を宣言(DEP-02)"
            )
        if _VAULT_OCID_SUBSTR_RE.search(value):
            raise DeploySpecError(
                f"environment '{key}' に Vault secret OCID。秘密は env に置かない(DEP-02 注入)"
            )
        out[key] = value
    return out


def _is_number(value: object) -> bool:
    """bool を除く数値(int/float)か。`True`/`False` を 1/0 として通さない。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_resources(app_port: int, ocpus: float, memory_gb: float) -> None:
    """コンテナのポート/サイズを妥当域に制限する(範囲外で tfvars を作らせない。fail-closed)。

    OCI Container Instance(CI.Standard.E4.Flex)は ocpu/メモリとも 1 以上の整数刻みが前提。
    端数(0.5 ocpu 等)や bool を弾き、apply 前に予測可能な ValueError にする。
    """
    if not isinstance(app_port, int) or isinstance(app_port, bool):
        raise DeploySpecError("app_port は整数で与えてください")
    if not 1 <= app_port <= MAX_PORT:
        raise DeploySpecError(f"app_port は 1..{MAX_PORT} の範囲で与えてください")
    if not _is_number(ocpus) or not (MIN_OCPUS <= ocpus <= MAX_OCPUS) or ocpus != int(ocpus):
        raise DeploySpecError(f"ocpus は {MIN_OCPUS}..{MAX_OCPUS} の整数で与えてください")
    if (
        not _is_number(memory_gb)
        or not (MIN_MEMORY_GB <= memory_gb <= MAX_MEMORY_GB)
        or memory_gb != int(memory_gb)
    ):
        raise DeploySpecError(f"memory_gb は {MIN_MEMORY_GB}..{MAX_MEMORY_GB} の整数で")


def _sanitize_prefix(prefix: str) -> str:
    """prefix(= K8s namespace/Deployment/**Service** 名)を健全化(英小文字数字とハイフン・長さ上限)。

    結果は **RFC 1035 ラベル**(`[a-z]([-a-z0-9]*[a-z0-9])?`・<=63)に適合させる。namespace は
    RFC 1123(先頭数字可)だが Service 名は RFC 1035 で **先頭英字必須**。両方に使うため厳しい
    RFC 1035 に合わせ、先頭が英字でなければ `j` を前置する(数字始まり prefix の失敗を防ぐ)。
    空なら fail-closed。
    """
    cleaned = re.sub(r"[^a-z0-9-]+", "-", prefix.lower()).strip("-")
    # 先頭を英字に固定(RFC 1035)。前置後に長さ切り詰め→両端ハイフン除去の順で整える。
    if cleaned and not cleaned[0].isalpha():
        cleaned = "j" + cleaned
    truncated = cleaned[:MAX_PREFIX_LEN].strip("-")
    if not truncated or not truncated[0].isalpha():
        raise DeploySpecError("prefix から有効な namespace/Service 名(RFC 1035)を作れません")
    return truncated


def _sanitize_label_value(value: str) -> str:
    """K8s label 値へ健全化(`[A-Za-z0-9]` 始終・中間に `-_.`・<=63)。作れなければ空文字。

    label 値の規約から外れると `kubectl apply` が弾くため、決定的に安全な値へ丸める。空入力や
    記号のみは空(= label を付けない)に倒す。
    """
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-_.")
    return cleaned[:63].strip("-_.")


class _NoAliasDumper(yaml.SafeDumper):
    """YAML の anchor/alias(`&id001`/`*id001`)を抑止する Dumper。

    labels() 等で同一 dict を複数 manifest/Pod template に渡すと、既定の安全 dumper は anchor/alias
    を出力する。後段の post-process が一部 labels を書き換えると alias 経由で他リソースの labels
    まで変わる(再利用の落とし穴)。各 labels を独立した平文として出すため alias を切る。
    """

    def ignore_aliases(self, data: object) -> bool:  # noqa: ARG002
        return True


def _dump_manifests(manifests: list[dict]) -> str:
    """マニフェスト群を決定的な複数ドキュメント YAML に直列化する(キーソート)。

    `kubectl apply -f` 可能な形へ。`sort_keys=True` で出力を決定的にし、`_NoAliasDumper` で
    anchor/alias を抑止する(同一 dict 再利用が YAML 上の参照共有にならないようにする)。
    """
    return "---\n".join(
        yaml.dump(m, Dumper=_NoAliasDumper, sort_keys=True,
                  default_flow_style=False, allow_unicode=True)
        for m in manifests
    )


def dump_manifests(manifests: list[dict]) -> str:
    """複数 K8s マニフェスト(dict のリスト)を決定的な複数ドキュメント YAML へ直列化する公開ヘルパ。

    `deploy_inject` の注入マニフェストや外部ツール(`tools/render_injection.py`)が private symbol に
    依存せず YAML 化できるよう公開する。`kubectl apply -f -` にそのまま流せる。
    """
    return _dump_manifests(manifests)


def build_deploy_spec(
    composition: DemoComposition,
    *,
    settings: Settings | None = None,
    image_url: str | None = None,
    prefix: str | None = None,
    tenant: str | None = None,
    plugin_id: str | None = None,
    sdk: str | None = None,
    required_secrets: Iterable[str] | None = None,
    extra_environment: Mapping[str, str] | None = None,
    ocpus: float = DEFAULT_OCPUS,
    memory_gb: float = DEFAULT_MEMORY_GB,
    app_port: int = DEFAULT_APP_PORT,
) -> ContainerDeploySpec:
    """合成済みデモ構成 → L3 デモ配備仕様(K8s)を決定的に生成する(副作用なし)。

    fail-closed:
      - `composition.ok` が False(合成不能)なら配備しない。
      - **デプロイ前ガバナンスゲートを常に内部実行**(`validate_governance(composition)`)。
        呼び出し側が report を渡してバイパス/詐称する余地を作らない。ok=False なら配備しない。
       - 秘密は **論理名の宣言のみ**(allowlist)。Vault OCID はマニフェスト/state に出さない。
      - `image_url` は OCIR(`kix.ocir.io/<ns>/<repo>`。ADR-0011)のみ。非 OCIR は拒否。

    マルチテナンシ(ADR-0016 §6): `tenant`(Project OCID 等)を渡すと、その **非秘密ハッシュ
    (`TENANT_HASH_LEN`=12 hex)**を namespace/prefix に必ず含め(`jetuse-demo-<app>-<tenantN>`)、
    `jetuse.dev/tenant` label にも付ける。
    これにより **同一 sample_app を別テナントへ配備しても namespace/Secret/Deployment が衝突しない**
    (`kubectl delete namespace` も他テナントを巻き込まない)。`tenant` 無指定はシングルテナント前提。
    """
    # 内部でガバナンスを評価するため遅延 import(top-level は循環回避で TYPE_CHECKING のみ)。
    from .governance import validate_governance

    settings = settings or get_settings()

    # 配備先リージョン整合(ADR-0011)。image=kix(ap-osaka)・jetuse-dev も ap-osaka なのに、
    # コンテナへ渡す OCI_REGION が別リージョンだと推論/Vault 解決が配備先とズレる。fail-closed。
    if settings.oci_region != DEPLOY_REGION:
        raise DeploySpecError(
            f"oci_region は {DEPLOY_REGION} 固定です(ADR-0011)。実値={settings.oci_region}"
        )

    if not composition.ok:
        detail = "; ".join(composition.errors) if composition.errors else "ok=False"
        raise DeploySpecError(f"合成不能な構成(composition.ok=False)は配備できません: {detail}")

    # デプロイ前ゲート(常に内部評価)。stale/詐称 report によるバイパスを構造的に不可能にする。
    if not validate_governance(composition).ok:
        raise DeploySpecError("ガバナンス未通過(デプロイ前ゲート違反)の構成は配備できません")

    _validate_resources(app_port, ocpus, memory_gb)

    image = (image_url or settings.hosted_demo_image_url or "").strip()
    if not image:
        raise DeploySpecError(
            "image_url 未指定(引数 image_url か settings.hosted_demo_image_url を与えてください)"
        )
    if not _OCIR_IMAGE_RE.match(image):
        raise DeploySpecError(
            f"image_url は ap-osaka OCIR(kix.ocir.io/<ns>/<repo>。ADR-0011): {image}"
        )

    # SDK 解決(ADR-0009)。明示 sdk は正準値(openai_agents|langgraph|adk)のみ受ける。未知/typo/
    # 非 hosted ランタイム(select_ai 等)が別 SDK へ黙って化けないよう fail-closed。未指定は既定。
    if sdk is not None and sdk not in _SDK_APP_OCID_ATTR:
        raise DeploySpecError(
            f"未知の sdk '{sdk}'。{sorted(_SDK_APP_OCID_ATTR)} のいずれかで与えてください"
        )
    resolved_sdk = normalize_sdk(sdk)
    agent_app_ocid = resolve_agent_app_ocid(resolved_sdk, settings)

    required_scopes = _collect_required_scopes(composition)
    active_connectors = tuple(composition.active_connectors)

    # 非秘密 env(決定的)。公開メタのみ。秘密は env に載せない(required_secrets で宣言)。
    env: dict[str, str] = {
        "OCI_REGION": settings.oci_region,
        "JETUSE_SDK": resolved_sdk,
        "JETUSE_DEMO_APP": (composition.app_name or composition.sample_app or "demo"),
        "JETUSE_ACTIVE_CONNECTORS": ",".join(active_connectors),
        "JETUSE_PLATFORM_SCOPES": ",".join(required_scopes),
    }
    if agent_app_ocid:
        env["JETUSE_AGENT_APP_OCID"] = agent_app_ocid
    # 追加 env は検証済み(非秘密・予約キー保護・名前形式・名前空間 allowlist)のものだけ載せる。
    env.update(_validate_extra_environment(extra_environment))
    if len(env) > MAX_ENV_VARS:
        raise DeploySpecError(f"環境変数が多すぎます(>{MAX_ENV_VARS})")
    # 生成値も含め最終 env のどの値にも Vault OCID を残さない(app_name/agent OCID 等の経路も塞ぐ)。
    for k, v in env.items():
        if _VAULT_OCID_SUBSTR_RE.search(v):
            raise DeploySpecError(f"environment '{k}' に Vault OCID。秘密は env に置かない(DEP-02)")
    env = dict(sorted(env.items()))

    secrets = _validate_required_secrets(required_secrets)

    # マルチテナント一意化: tenant を渡したら非秘密ハッシュ(12hex)を prefix/namespace に必ず含める。
    # これで同一 sample_app を別テナントに配備しても namespace/Secret/Deployment が衝突しない。
    tenant_hash = ""
    base_prefix = prefix or f"jetuse-demo-{composition.sample_app or 'app'}"
    # tenant 未指定(None)はシングルテナント。**明示の空/空白のみは fail-closed**(環境変数展開ミスで
    # 複数テナントが同一 namespace/Secret に集約される事故を防ぐ。tenant 分離の前提を守る)。
    if tenant is not None:
        # 前後空白を正規化してから判定・ハッシュ化する。空白付き OCID で namespace が分裂したり、
        # grant 側(前後空白を拒否)と食い違って no_grant になる境界を防ぐ(review 対応)。
        tenant = tenant.strip()
        if not tenant:
            raise DeploySpecError("tenant が空(空白のみ)。テナント分離に有効な識別子を渡すこと")
        tenant_hash = tenant_hash_hex(tenant)
        # tenant suffix(`-<hash8>`=9 文字)が長さ上限の切り詰めで欠落しないよう、**base を先に
        # 正規化＋ suffix 分を残して切り詰めてから** 完全な suffix を付ける。長い prefix/sample_app
        # でも最終 prefix が必ず tenant ハッシュで終わる(テナント間で衝突しない)。
        suffix = f"-{tenant_hash}"
        base_clean = _sanitize_prefix(base_prefix)[: MAX_PREFIX_LEN - len(suffix)].rstrip("-")
        spec_prefix = _sanitize_prefix(f"{base_clean}{suffix}")
        if not spec_prefix.endswith(suffix):
            raise DeploySpecError("tenant ハッシュを prefix 末尾に付与できません(命名衝突の恐れ)")
    else:
        spec_prefix = _sanitize_prefix(base_prefix)

    # 発行主体プラグイン(承認グラントのキー)。指定時は正規化して Deployment 注釈へ固定する
    # (注入が別プラグインのグラントへすり替えるのを ground truth で防ぐ)。空/空白は付けない。
    plugin_norm = plugin_id.strip() if plugin_id else ""

    return ContainerDeploySpec(
        region=DEPLOY_REGION,
        prefix=spec_prefix,
        image_url=image,
        app_port=app_port,
        ocpus=float(ocpus),
        memory_gb=float(memory_gb),
        sdk=resolved_sdk,
        agent_app_ocid=agent_app_ocid,
        environment_variables=env,
        required_secrets=secrets,
        required_scopes=required_scopes,
        active_connectors=active_connectors,
        sample_app=_sanitize_label_value(composition.sample_app or ""),
        tenant_hash=tenant_hash,
        plugin_id=plugin_norm,
    )


__all__ = [
    "ContainerDeploySpec",
    "DeploySpecError",
    "build_deploy_spec",
    "dump_manifests",
    "resolve_agent_app_ocid",
]
