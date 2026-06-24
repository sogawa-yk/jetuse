"""マネージド・ホスト型エージェント連携(GAP-04)。

OCI Hosted Applications/Deployments(AGT-04で実証)へ、アプリから invoke する。
- 認証: IDCSの client_credentials トークン(aud/scope一致、プロセス内キャッシュ+期限更新)
- invoke URL(未文書・規則ベース、AGT-04実機確定):
  https://inference.generativeai.{region}.oci.oraclecloud.com/20251112/hostedApplications/{APP}/actions/invoke/{path}
- サンプルエージェントの契約: POST /invoke {"input": str} -> {"output": str}（非ストリーミング）

設定(.env / tfvars。未設定なら framework=hosted は503):
  HOSTED_AGENT_APP_OCID / HOSTED_AGENT_IDCS_DOMAIN / HOSTED_AGENT_CLIENT_ID /
  HOSTED_AGENT_CLIENT_SECRET / HOSTED_AGENT_SCOPE
"""

import logging
import threading
import time

import httpx

from .settings import get_settings

logger = logging.getLogger("jetuse.hosted_agent")

_token: dict = {"value": None, "exp": 0.0}
_lock = threading.Lock()


class HostedAgentNotConfigured(RuntimeError):
    pass


def _require_config():
    s = get_settings()
    missing = [
        k for k in ("hosted_agent_app_ocid", "hosted_agent_idcs_domain",
                    "hosted_agent_client_id", "hosted_agent_client_secret",
                    "hosted_agent_scope")
        if not getattr(s, k)
    ]
    if missing:
        raise HostedAgentNotConfigured(f"hosted agent not configured: {missing}")
    return s


def _get_token(s) -> str:
    """client_credentialsトークン(期限60秒前まで再利用)"""
    now = time.time()
    if _token["value"] and now < _token["exp"] - 60:
        return _token["value"]
    with _lock:
        if _token["value"] and now < _token["exp"] - 60:
            return _token["value"]
        r = httpx.post(
            f"{s.hosted_agent_idcs_domain}/oauth2/v1/token",
            auth=(s.hosted_agent_client_id, s.hosted_agent_client_secret),
            data={"grant_type": "client_credentials", "scope": s.hosted_agent_scope},
            timeout=30,
        )
        r.raise_for_status()
        body = r.json()
        _token["value"] = body["access_token"]
        _token["exp"] = now + int(body.get("expires_in", 3600))
        return _token["value"]


def invoke(text: str, path: str = "invoke") -> str:
    """(旧GAP-04互換)ホスト型エージェントへ単発入力を送り出力テキストを返す。"""
    s = _require_config()
    token = _get_token(s)
    base = (
        f"https://inference.generativeai.{s.oci_region}.oci.oraclecloud.com"
        f"/20251112/hostedApplications/{s.hosted_agent_app_ocid}/actions/invoke/{path}"
    )
    r = httpx.post(
        base,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"input": text[:8000]},
        timeout=120,
    )
    r.raise_for_status()
    data = r.json()
    return data.get("output") or data.get("result") or ""


# AGT-MULTI(ADR-0009): SDK選択 -> Application OCID
_SDK_ATTR = {
    "openai_agents": "agent_openai_app_ocid",
    "langgraph": "agent_langgraph_app_ocid",
    "adk": "agent_adk_app_ocid",
}
# 旧framework値からの後方互換マッピング
_LEGACY_SDK = {
    "agents_sdk": "openai_agents", "native": "openai_agents",
    "hosted": "openai_agents", "openai_agents": "openai_agents",
    "langgraph": "langgraph", "adk": "adk",
}


def normalize_sdk(framework: str | None) -> str:
    return _LEGACY_SDK.get(framework or "", "openai_agents")


def invoke_agent(sdk: str, state: dict) -> dict:
    """SDK選択に応じたホスト型ReActコンテナへステート(system_prompt/enabled_tools/
    input/history/rag_store_id/model)を送り、{output, tool_trace, sdk} を返す。"""
    s = get_settings()
    missing = [
        k for k in ("hosted_agent_idcs_domain", "hosted_agent_client_id",
                    "hosted_agent_client_secret", "hosted_agent_scope")
        if not getattr(s, k)
    ]
    attr = _SDK_ATTR.get(sdk)
    app_ocid = getattr(s, attr) if attr else ""
    if missing or not app_ocid:
        raise HostedAgentNotConfigured(
            f"agent container not configured: sdk={sdk} missing={missing or 'app_ocid'}")
    token = _get_token(s)
    url = (
        f"https://inference.generativeai.{s.oci_region}.oci.oraclecloud.com"
        f"/20251112/hostedApplications/{app_ocid}/actions/invoke/invoke"
    )
    r = httpx.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=state,
        timeout=180,
    )
    r.raise_for_status()
    return r.json()
