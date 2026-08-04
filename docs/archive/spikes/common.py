"""スパイク共通: OCI GenAI OpenAI互換クライアント生成。

認証は ~/.oci/config の IAM 署名（oci-genai-auth）。
環境依存値は リポジトリ直下の .env から読む。
"""
import os
from pathlib import Path

import httpx
from openai import OpenAI
from oci_genai_auth import OciUserPrincipalAuth

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_env() -> dict:
    env = {}
    for line in (REPO_ROOT / ".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k] = v
    return env


ENV = load_env()
REGION = ENV["OCI_REGION"]
COMPARTMENT_ID = ENV["COMPARTMENT_OCID"]

BASE_URL_OPENAI = f"https://inference.generativeai.{REGION}.oci.oraclecloud.com/openai/v1"
BASE_URL_ACTIONS = f"https://inference.generativeai.{REGION}.oci.oraclecloud.com/20231130/actions/v1"
# Vector Store本体のCRUDはコントロールプレーン側（SPIKE-03で確認）
BASE_URL_CP = f"https://generativeai.{REGION}.oci.oraclecloud.com/20231130/openai/v1"


def make_client(base_url: str = BASE_URL_OPENAI, timeout: float = 120.0,
                with_project: bool = False) -> OpenAI:
    """OCI GenAI OpenAI互換クライアント。

    with_project=True で GenerativeAiProject OCID を OpenAi-Project ヘッダに付与する。
    Files / Vector Stores / Conversations 等の状態を持つAPIはProject必須（SPIKE-03で確認）。
    """
    headers = {"CompartmentId": COMPARTMENT_ID}
    if with_project:
        headers["OpenAi-Project"] = ENV["PROJECT_OCID"]
    return OpenAI(
        api_key="OCI",  # ダミー。実認証はhttpxのIAM署名
        base_url=base_url,
        http_client=httpx.Client(
            auth=OciUserPrincipalAuth(),
            headers=headers,
            timeout=timeout,
        ),
    )


def make_cp_client(timeout: float = 120.0) -> OpenAI:
    """コントロールプレーン用クライアント（Vector Store本体のCRUD）。

    ヘッダは opc-compartment-id。Projectヘッダ不要。
    """
    return OpenAI(
        api_key="OCI",
        base_url=BASE_URL_CP,
        http_client=httpx.Client(
            auth=OciUserPrincipalAuth(),
            headers={"opc-compartment-id": COMPARTMENT_ID},
            timeout=timeout,
        ),
    )
