"""OCI Enterprise AI OpenAI互換クライアント生成(spikes/common.py から昇格)。

specs/00 の未文書仕様に対応:
- ホスト2系統: 推論(DP)とVector Store本体CRUD(CP)
- 状態APIは OpenAi-Project ヘッダ(GenerativeAiProject OCID) + CompartmentId 必須

ローカル/devインスタンスではIAMユーザー署名(~/.oci/config)。
CI/Functions上ではリソースプリンシパルに切り替える(INFRA-01 apply後に実装 — TODO)。
"""

import os

import httpx
from oci_genai_auth import OciResourcePrincipalAuth, OciUserPrincipalAuth
from openai import OpenAI

from .settings import Settings, get_settings


def _signer():
    # CI/Functions上は AUTH_MODE=resource_principal を環境変数で指定(specs/07)
    if os.environ.get("AUTH_MODE") == "resource_principal":
        return OciResourcePrincipalAuth()
    return OciUserPrincipalAuth()


def make_inference_client(
    settings: Settings | None = None,
    *,
    with_project: bool = False,
    timeout: float = 120.0,
    project_ocid: str | None = None,
) -> OpenAI:
    """推論系(Responses / Chat Completions / Files / Conversations / File Search)。

    project_ocid指定でエージェントのProject分離(AGT-03)に対応。
    """
    settings = settings or get_settings()
    # Chat Completions は CompartmentId、Responses API は opc-compartment-id を要求するため両方送る
    # (Responses APIは CompartmentId だけだと 400 "Compartment ID must be provided" — 実機確定)
    headers = {
        "CompartmentId": settings.compartment_ocid,
        "opc-compartment-id": settings.compartment_ocid,
    }
    if with_project:
        headers["OpenAi-Project"] = project_ocid or settings.project_ocid
    return OpenAI(
        api_key="OCI",  # ダミー。実認証はhttpxのIAM署名
        base_url=settings.inference_base_url,
        http_client=httpx.Client(auth=_signer(), headers=headers, timeout=timeout),
    )


def make_cp_client(settings: Settings | None = None, *, timeout: float = 120.0) -> OpenAI:
    """Vector Store本体CRUD(CP)。ヘッダは opc-compartment-id、Project不要。"""
    settings = settings or get_settings()
    return OpenAI(
        api_key="OCI",
        base_url=settings.cp_base_url,
        http_client=httpx.Client(
            auth=_signer(),
            headers={"opc-compartment-id": settings.compartment_ocid},
            timeout=timeout,
        ),
    )


def make_cp_client_for(region: str, compartment_ocid: str, *,
                       timeout: float = 120.0) -> OpenAI:
    """台帳 locator 指定の CP クライアント(specs/18 §3.2 — 削除は台帳の全 locator で
    クライアントを構成して行う。現在の設定と不一致でも旧 target を消せるように)。"""
    return OpenAI(
        api_key="OCI",
        base_url=f"https://generativeai.{region}.oci.oraclecloud.com/20231130/openai/v1",
        http_client=httpx.Client(
            auth=_signer(),
            headers={"opc-compartment-id": compartment_ocid},
            timeout=timeout,
        ),
    )


def make_inference_client_for(region: str, compartment_ocid: str,
                              project_ocid: str, *, timeout: float = 120.0) -> OpenAI:
    """台帳 locator 指定の DP クライアント(Files 系の削除用)。"""
    return OpenAI(
        api_key="OCI",
        base_url=f"https://inference.generativeai.{region}.oci.oraclecloud.com/openai/v1",
        http_client=httpx.Client(
            auth=_signer(),
            headers={
                "CompartmentId": compartment_ocid,
                "opc-compartment-id": compartment_ocid,
                "OpenAi-Project": project_ocid,
            },
            timeout=timeout,
        ),
    )
