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


_SDK_LABEL = {"openai_agents": "OpenAI Agents SDK", "langgraph": "LangGraph", "adk": "ADK"}

_OAUTH_KEYS = ("hosted_agent_idcs_domain", "hosted_agent_client_id",
               "hosted_agent_client_secret", "hosted_agent_scope")


def normalize_sdk(framework: str | None) -> str:
    return _LEGACY_SDK.get(framework or "", "openai_agents")


def availability() -> dict:
    """ホスト型エージェントの配備状況(PORT-03)。

    /api/health の capabilities.agents と、実行時の縮退メッセージが同じ判定を共有する。
    利用者に見せるのは `agent container not configured: missing=[...]` のような内部文字列
    ではなく、「なぜ使えないか・どうすれば使えるか」の理由にする(PORT-02 の方針)。
    """
    s = get_settings()
    oauth_missing = [k for k in _OAUTH_KEYS if not getattr(s, k)]
    # 「配備済み」と「実際に呼べる」は別物。OAuth 資格情報が欠けていれば Application OCID が
    # あっても invoke できないので、sdks は**呼べるか**で答える(review F-011)。
    deployed = {sdk: bool(getattr(s, attr)) for sdk, attr in _SDK_ATTR.items()}
    sdks = {sdk: (not oauth_missing) and ok for sdk, ok in deployed.items()}
    ready = [sdk for sdk, ok in sdks.items() if ok]

    if oauth_missing or not ready:
        # 認証が無効なスタックには OAuth(client_credentials)の発行元が存在しない。
        # そこだけは原因が確定できるので、汎用の案内と区別して出す。
        cause = (
            "OIDC認証が無効なため、エージェント呼び出しに使う OAuth の発行元(Identity Domain)が"
            "ありません。認証を有効にして再デプロイしてください"
            if not s.auth_required else
            "スタック変数 enable_hosted_agents と、デプロイ先リージョン"
            "(配備対象は 大阪 ap-osaka-1 / シカゴ us-chicago-1)をご確認ください"
        )
        return {"ok": False, "sdks": sdks, "deployed": deployed,
                "reason": f"このスタックにはホスト型エージェントが配備されていません。{cause}"}

    if len(ready) < len(sdks):
        missing_labels = "・".join(_SDK_LABEL[s_] for s_, ok in sdks.items() if not ok)
        return {"ok": True, "sdks": sdks, "deployed": deployed,
                "reason": f"一部のSDK({missing_labels})が配備されていません"}

    return {"ok": True, "sdks": sdks, "deployed": deployed, "reason": None}


def invoke_agent(sdk: str, state: dict) -> dict:
    """SDK選択に応じたホスト型ReActコンテナへステート(system_prompt/enabled_tools/
    input/history/rag_store_id/model)を送り、{output, tool_trace, sdk} を返す。"""
    s = get_settings()
    missing = [k for k in _OAUTH_KEYS if not getattr(s, k)]
    attr = _SDK_ATTR.get(sdk)
    app_ocid = getattr(s, attr) if attr else ""
    if missing or not app_ocid:
        # 内部の欠落キー名は診断用にログへ、利用者へは理由と対処を返す(PORT-03)。
        logger.warning("hosted agent not configured: sdk=%s missing=%s app_ocid=%s",
                       sdk, missing, bool(app_ocid))
        label = _SDK_LABEL.get(sdk, sdk)
        reason = availability()["reason"] or f"{label} のエージェントが配備されていません"
        raise HostedAgentNotConfigured(f"{reason}（SDK: {label}）")
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
