"""OCI へのログイン（署名 / SDK クライアント引数）の単一リゾルバ。
全モジュールはここを経由する（従来 ~19 箇所に散っていた `if AUTH_MODE == "resource_principal"`
分岐を1箇所へ集約）。※ ユーザー認証(OIDC/JWT)は別モジュール `auth.py`。

モード（env AUTH_MODE を call-time 優先、無ければ settings.auth_mode、既定 config_file）:
- config_file        : ~/.oci/config のプロファイル（ローカル開発の既定）
- resource_principal : 配備済みサービスのリソースプリンシパル（Terraform が env で設定）
- instance_principal : OCI インスタンス自身のプリンシパル
"""

import os

from oci_genai_auth import (
    OciInstancePrincipalAuth,
    OciResourcePrincipalAuth,
    OciUserPrincipalAuth,
)

from .settings import get_settings

_AUTH_MODE_HINT = (
    "OCI設定ファイル(~/.oci/config)が見つかりません。"
    "auth_mode / AUTH_MODE（config_file か resource_principal か）を確認してください"
)


def _mode() -> str:
    # env AUTH_MODE を call-time で尊重（従来挙動・後方互換）。無ければ settings.auth_mode。
    return os.environ.get("AUTH_MODE") or get_settings().auth_mode or "config_file"


def _profile() -> str | None:
    return get_settings().oci_profile or None


def load_local_oci_config(profile: str | None = None) -> dict:
    """~/.oci/config を読む（config_file モードのフォールバック）。未設定コンテナでの
    ConfigFileNotFound を原因明示の RuntimeError に変える（root-cause を1箇所に集約）。"""
    import oci

    prof = profile or _profile()
    try:
        return oci.config.from_file(profile_name=prof) if prof else oci.config.from_file()
    except oci.exceptions.ConfigFileNotFound as e:
        raise RuntimeError(_AUTH_MODE_HINT) from e


def httpx_auth():
    """Family A: OpenAI 互換 httpx への署名注入オブジェクト（oci_genai_auth）。"""
    mode = _mode()
    if mode == "resource_principal":
        return OciResourcePrincipalAuth()
    if mode == "instance_principal":
        return OciInstancePrincipalAuth()
    prof = _profile()
    try:
        return OciUserPrincipalAuth(profile_name=prof) if prof else OciUserPrincipalAuth()
    except Exception as e:
        import oci

        if isinstance(e, oci.exceptions.ConfigFileNotFound):
            raise RuntimeError(_AUTH_MODE_HINT) from e
        raise


def sdk_signer_args(region: str | None = None) -> dict:
    """Family B: 素の OCI SDK クライアント引数。使い方: SomeClient(**sdk_signer_args(region))。
    - resource_principal / instance_principal: {"config": {"region": region}, "signer": ...}
    - config_file: {"config": ~/.oci/config}（region はプロファイル値。config へ明示したい場合は
      呼び出し側で args["config"]["region"] = ... を設定する）。
    """
    import oci

    mode = _mode()
    if mode == "resource_principal":
        cfg = {"region": region} if region else {}
        return {"config": cfg, "signer": oci.auth.signers.get_resource_principals_signer()}
    if mode == "instance_principal":
        cfg = {"region": region} if region else {}
        return {"config": cfg, "signer": oci.auth.signers.InstancePrincipalsSecurityTokenSigner()}
    return {"config": load_local_oci_config()}
