"""OCI Enterprise AI OpenAI互換クライアント生成(spikes/common.py から昇格)。

specs/00 の未文書仕様に対応:
- ホスト2系統: 推論(DP)とVector Store本体CRUD(CP)
- 状態APIは OpenAi-Project ヘッダ(GenerativeAiProject OCID) + CompartmentId 必須

ローカル/devインスタンスではIAMユーザー署名(~/.oci/config)。
CI/Functions上ではリソースプリンシパルに切り替える(INFRA-01 apply後に実装 — TODO)。
"""

import logging
import os
import threading
import time

import httpx
from oci_genai_auth import OciResourcePrincipalAuth, OciUserPrincipalAuth
from openai import OpenAI

from .settings import Settings, get_settings

logger = logging.getLogger("jetuse.genai")

_AUTH_MODE_HINT = (
    "OCI設定ファイル(~/.oci/config)が見つかりません。"
    "AUTH_MODE=resource_principal の設定漏れの可能性があります"
)


def _signer():
    # CI/Functions上は AUTH_MODE=resource_principal を環境変数で指定(specs/07)
    if os.environ.get("AUTH_MODE") == "resource_principal":
        return OciResourcePrincipalAuth()
    try:
        # OciUserPrincipalAuth()内部でもoci.config.from_file()を呼ぶため、
        # ここで捕捉しないとload_local_oci_config()の外側で生ConfigFileNotFoundが
        # 漏れる(make_inference_client/make_cp_client/nl2sql等genai系全経路の入口 —
        # レビュー指摘: AUTH_MODEガードがOpenAI互換クライアント側に届いていなかった)。
        return OciUserPrincipalAuth()
    except Exception as e:
        import oci

        if isinstance(e, oci.exceptions.ConfigFileNotFound):
            raise RuntimeError(_AUTH_MODE_HINT) from e
        raise


def load_local_oci_config() -> dict:
    """AUTH_MODE!=resource_principal のときの ~/.oci/config フォールバック(PORT-02)。

    未設定コンテナでの ConfigFileNotFound を未処理500で落とさず、原因(AUTH_MODE設定漏れ)を
    明示する。genai/obs/tts/stt_realtime/docunderstand/minutes/guardrails/embeddings/
    mcp_servers/agents/rag/translate/db の各モジュールが共通で使う(root-causeを1箇所に集約)。
    """
    import oci

    try:
        return oci.config.from_file()
    except oci.exceptions.ConfigFileNotFound as e:
        raise RuntimeError(_AUTH_MODE_HINT) from e


# --- GenerativeAiProject 解決(FIX-47 / Issue #47) ---
# DP 状態API(Files / Vector Store files / Conversations / Responses)は OpenAi-Project ヘッダ必須
# (specs/00 未文書仕様)。未設定のまま空ヘッダを送ると別テナンシで必ず落ちるため、
# 設定 > プロセス内キャッシュ > compartment内ACTIVE検索 > 自動作成 の順に解決し、
# 解決不能なら actionable なメッセージで即時 raise する(空ヘッダは送らない)。


class ProjectResolutionError(Exception):
    """OpenAi-Project に入れる GenerativeAiProject OCID が解決できない。"""


_ACTIONABLE = (
    "GenerativeAI project を解決できません(RAG / Responses / 会話メモリに必須)。"
    "スタック変数または環境変数 PROJECT_OCID を設定するか、PROJECT_AUTOCREATE=true と "
    "'manage generative-ai-project' の IAM policy で自動作成を許可してください"
    "(DG matching rule / リージョンの agentic API 対応も確認)"
)

_project_lock = threading.Lock()
_project_cache: str | None = None


def _reset_project_cache() -> None:
    global _project_cache
    _project_cache = None


def _sdk_client(settings: Settings):
    """GenerativeAiClient(CP)。project は推論リージョンと同一リージョンに置く
    (project OCID はリージョン別 — docs/tips.md)。"""
    import oci

    if os.environ.get("AUTH_MODE") == "resource_principal":
        signer = oci.auth.signers.get_resource_principals_signer()
        return oci.generative_ai.GenerativeAiClient(
            {"region": settings.oci_region}, signer=signer
        )
    config = load_local_oci_config()
    config["region"] = settings.oci_region
    return oci.generative_ai.GenerativeAiClient(config)


def _create_project(client, settings: Settings) -> str:
    """project を自動作成し ACTIVE を有界待ち。非 ACTIVE のまま返すと OpenAi-Project が
    404 になるため、ACTIVE に達しなければ raise(キャッシュもしない — REV-001 major#2)。"""
    import oci

    details = oci.generative_ai.models.CreateGenerativeAiProjectDetails(
        compartment_id=settings.compartment_ocid,
        display_name="jetuse-project",
        description="auto-created by JetUse (FIX-47)",
    )
    created = client.create_generative_ai_project(details).data
    for _ in range(15):
        state = getattr(created, "lifecycle_state", "")
        if state == "ACTIVE":
            logger.info("generative-ai project auto-created")
            return created.id
        if state in ("FAILED", "DELETING", "DELETED"):
            break
        time.sleep(2)
        created = client.get_generative_ai_project(created.id).data
    raise ProjectResolutionError(
        _ACTIONABLE + f" (cause: auto-created project stuck in "
        f"{getattr(created, 'lifecycle_state', '?')})"
    )


def resolve_project_ocid(
    settings: Settings | None = None, *, allow_autocreate: bool = True
) -> str:
    """OpenAi-Project 用 project OCID を返す。

    設定 > キャッシュ > compartment内ACTIVE検索 > 自動作成(PROJECT_AUTOCREATE=true のときのみ。
    公開 ORM スタックが policy とセットで有効化する — ベアランタイム既定は検出のみ)。

    allow_autocreate=False は診断/health目的の呼び出し向け(PORT-02): GETの読み取り専用
    エンドポイントがポーリングだけでリソースを作ってしまうのを避ける(レビュー指摘)。
    """
    global _project_cache
    settings = settings or get_settings()
    if settings.project_ocid:
        return settings.project_ocid
    if _project_cache:
        return _project_cache
    with _project_lock:
        if _project_cache:
            return _project_cache
        try:
            import oci

            client = _sdk_client(settings)
            # 全ページ取得(1ページ目に ACTIVE が無いだけで新規作成しない — REV-001 major#1)
            items = oci.pagination.list_call_get_all_results(
                client.list_generative_ai_projects, settings.compartment_ocid
            ).data
            resolved = next((p.id for p in items if p.lifecycle_state == "ACTIVE"), None)
            if not resolved:
                if not settings.project_autocreate or not allow_autocreate:
                    raise ProjectResolutionError(
                        _ACTIONABLE + " (cause: no ACTIVE project and autocreate disabled)"
                    )
                resolved = _create_project(client, settings)
        except ProjectResolutionError:
            raise
        except Exception as e:
            status = getattr(e, "status", None)
            code = getattr(e, "code", None) or type(e).__name__
            suffix = f" (cause: {code}{f' HTTP {status}' if status else ''})"
            raise ProjectResolutionError(_ACTIONABLE + suffix) from e
        _project_cache = resolved
        return resolved


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
        # 解決不能なら ProjectResolutionError(空の OpenAi-Project は送らない — FIX-47)
        headers["OpenAi-Project"] = project_ocid or resolve_project_ocid(settings)
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
