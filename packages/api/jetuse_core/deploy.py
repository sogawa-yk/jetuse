"""DEP-01: 生成デモのコンテナ配備(L3 ホスト型)仕様生成。

合成済み・ガバナンス ok の `DemoComposition`(synth.py)から、既存の container-instance Terraform
モジュール(`infra/terraform/modules/container-instance`)へそのまま渡せる
**宣言的なコンテナ配備仕様** を **決定的に** 生成する。新規インフラのプロビジョニングはしない
(D8: デプロイ上限=コンテナ)。出力はアプリ層成果物(tfvars)であり、固定基盤の保証がそのまま効く。

再利用(新規の実行基盤・認可経路は作らない):
  - ADR-0009: SDK→ホスト型 Application OCID 解決
    (`hosted_agent.normalize_sdk` / 設定 `agent_*_app_ocid`)。
  - ADR-0011: 配備イメージは OCIR(ap-osaka-1, public)。public のため image_pull_secret は既定不要。
  - Platform API ブローカー(ADR-0014/D5): デモコンテナは **DB 資格情報を持たず**、ブローカー発行の
    スコープ付き短期トークンでテナントデータへ到達する。配備仕様には付与予定の `required_scopes`
    のみを記録する(実トークンは持たせない。実注入の本実装は DEP-02)。

セキュリティ姿勢: **fail-closed** かつ **秘密(実値も OCID 参照も)を tfvars/state に残さない**。
  - composition.ok でない/ガバナンス未通過の構成は配備仕様を作らない(DeploySpecError)。
  - 秘密は **「要求する秘密の論理名」だけを宣言**する(`required_secrets`、allowlist 制約)。
    具体的な Vault secret OCID の解決と注入は **DEP-02(Platform API 注入)** の責務であり、本仕様は
    Vault OCID を一切持たない・tfvars にも出さない(Terraform state へ機微な参照先を永続化しない)。
  - コンテナ env(`environment_variables`)は **非秘密のみ**(キーは OCI_REGION/JETUSE_*)。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

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

# 上限(暴走・巨大 tfvars 防止。store/scaffold と同方針)。
MAX_ENV_VARS = 100
MAX_REQUIRED_SECRETS = 50
MAX_PREFIX_LEN = 40

# build_deploy_spec が必ず生成する予約 env キー。extra_environment での上書きを禁じる。
_RESERVED_ENV_KEYS = frozenset({
    "OCI_REGION",
    "JETUSE_SDK",
    "JETUSE_DEMO_APP",
    "JETUSE_ACTIVE_CONNECTORS",
    "JETUSE_PLATFORM_SCOPES",
    "JETUSE_AGENT_APP_OCID",
})

# L3 デモコンテナが**要求してよい秘密(論理名)の許可リスト**(ADR-0014/D5: 越境・最小権限)。
# デモコンテナは DB 資格情報も broker 署名鍵も持たず、テナントデータへは Platform 発行の
# 短期トークンでのみ到達する。コンテナ自身が短期トークンを取得するための OIDC クライアント資格だけが
# 正当な要求秘密。それ以外(DB_PASSWORD / ADB_WALLET_PASSWORD / 他)
# は **すべて fail-closed で拒否**(denylist でなく allowlist)。追加はレビュー要。
# **本仕様は秘密の論理名のみ宣言**。Vault OCID 解決・注入は DEP-02(OCID は持たない)。
_ALLOWED_SECRET_NAMES = frozenset({
    "HOSTED_AGENT_CLIENT_SECRET",   # コンテナの OIDC クライアント秘密(短期トークン取得用)
})

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
    """L3 デモコンテナの宣言的配備仕様。container-instance モジュール変数へ 1:1 で写像できる。

    `environment_variables` は **非秘密のみ**(キーは OCI_REGION か JETUSE_*)。秘密は値も OCID 参照も
    本仕様・tfvars・state に持たない。代わりに `required_secrets`(論理名)だけを残し、
    具体的な Vault OCID 解決・コンテナ注入は **DEP-02(Platform API 注入)** が担う。
    """

    #: 配備先リージョン(ADR-0011: ap-osaka-1 固定)。tfvars にも明示出力して provider を固定する。
    region: str
    #: container-instance の display_name 接頭辞(`<prefix>-api`)。
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
    #: 具体的な解決と注入は DEP-02。本仕様/tfvars/state には機微な参照先を残さない。
    required_secrets: tuple[str, ...]
    #: ブローカーから付与予定の Platform スコープ(active コネクタ由来の和集合・ソート済み)。
    required_scopes: tuple[str, ...]
    #: invoke 経路へ載る active コネクタ provider(記録用)。
    active_connectors: tuple[str, ...]

    def __post_init__(self) -> None:
        # frozen dataclass の不変性を環境変数 map にも及ぼす(構築後の差し込みを防ぐ)。
        object.__setattr__(
            self, "environment_variables", MappingProxyType(dict(self.environment_variables))
        )

    def module_environment(self) -> dict[str, str]:
        """container-instance モジュールへ渡す環境変数(**非秘密のみ**・決定的)。

        秘密はここに合流させない(Vault OCID を state に残さないため)。秘密注入は DEP-02。
        """
        return dict(sorted(self.environment_variables.items()))

    def to_tfvars(self) -> dict:
        """hosted-demo Terraform 環境(environments/hosted-demo)の変数へ写像した dict。

        コンパートメント/サブネット/NSG は **基盤側(固定リファレンス基盤)** が供給する infra 値で、
        構成からは決まらないため含めない(env 側の tfvars/`TF_VAR_` で与える)。**秘密(Vault OCID)は
        含めない**(`required_secrets` は仕様側のメタで、tfvars/state には出さない=DEP-02 が扱う)。
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
        """`*.auto.tfvars.json` 用の決定的 JSON(キーソート)。秘密値・Vault OCID なし。"""
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
    if not _is_number(memory_gb) or not (MIN_MEMORY_GB <= memory_gb <= MAX_MEMORY_GB) \
            or memory_gb != int(memory_gb):
        raise DeploySpecError(f"memory_gb は {MIN_MEMORY_GB}..{MAX_MEMORY_GB} の整数で")


def _sanitize_prefix(prefix: str) -> str:
    """display_name 接頭辞を健全化(英数とハイフンのみ・長さ上限)。空なら fail-closed。"""
    cleaned = re.sub(r"[^a-z0-9-]+", "-", prefix.lower()).strip("-")
    # 長さ切り詰め後に末尾ハイフンを除くと再び空になる入力もあるため、最終結果で再検査する。
    truncated = cleaned[:MAX_PREFIX_LEN].strip("-")
    if not truncated:
        raise DeploySpecError("prefix から有効な display_name を作れません")
    return truncated


def build_deploy_spec(
    composition: DemoComposition,
    *,
    settings: Settings | None = None,
    image_url: str | None = None,
    prefix: str | None = None,
    sdk: str | None = None,
    required_secrets: Iterable[str] | None = None,
    extra_environment: Mapping[str, str] | None = None,
    ocpus: float = DEFAULT_OCPUS,
    memory_gb: float = DEFAULT_MEMORY_GB,
    app_port: int = DEFAULT_APP_PORT,
) -> ContainerDeploySpec:
    """合成済みデモ構成 → L3 デモコンテナ配備仕様を決定的に生成する(副作用なし)。

    fail-closed:
      - `composition.ok` が False(合成不能)なら配備しない。
      - **デプロイ前ガバナンスゲートを常に内部実行**(`validate_governance(composition)`)。
        呼び出し側が report を渡してバイパス/詐称する余地を作らない。ok=False なら配備しない。
       - 秘密は **論理名の宣言のみ**(allowlist)。Vault OCID は tfvars/state に出さない。
      - `image_url` は OCIR(`kix.ocir.io/<ns>/<repo>`。ADR-0011)のみ。非 OCIR は拒否。
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
        raise DeploySpecError(
            "ガバナンス未通過(デプロイ前ゲート違反)の構成は配備できません"
        )

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

    spec_prefix = _sanitize_prefix(
        prefix or f"jetuse-demo-{composition.sample_app or 'app'}"
    )

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
    )


__all__ = [
    "ContainerDeploySpec",
    "DeploySpecError",
    "build_deploy_spec",
    "resolve_agent_app_ocid",
]
