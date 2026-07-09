"""ORASEJAPAN 共有テナンシ署名材料の OCI Vault 取得(SP3-09 — デプロイ環境の経路)。

RM sensitive 変数経由の鍵材料(SP3-07 の GEN_SHARED_KEY_PEM_B64 等)を廃し、jetuse:dev の
Vault シークレット(JSON: user/tenancy/fingerprint/region/key_pem)へ移す。API はリソース
プリンシパルで取得し、署名は in-memory で構成する(鍵をディスク・環境変数に出さない)。

- 設定は GEN_SHARED_SECRET_OCID 1 個(非鍵材料)。未設定 = この経路は無効。
  ローカル開発は従来どおり GEN_SHARED_PROFILE(~/.oci)— sign_proxy._route が優先順位を持つ。
- 取得/解析/署名構成の失敗は fail-closed: None を返し共有モデルのみ 403(API は起動継続)。
  成功はプロセス生涯キャッシュ。失敗は短いバックオフ(_FAIL_BACKOFF_S)だけ負キャッシュする —
  Vault 障害中に共有モデルへのリクエストごと同期フェッチが走ってイベントループを占有しない
  (codex review-1 M001)。seed 前の placeholder "{}" はバックオフ経過後の次リクエストで
  再起動なしに有効化される。値はログに出さない。
- 呼び出し規約: get_auth はブロッキング(OCI SDK 同期呼び出し)— async 経路からは
  asyncio.to_thread 経由で呼ぶ(sign_proxy.proxy がそうしている)。コールドスタート時の
  並行呼び出しはロックで single-flight 化(フェッチは 1 回 — review-2 m002)。
- ローテーション: user principal(API 鍵)は期限切れしないため周期/401 の自動再取得は
  持たない(いずれも同期 Vault I/O をイベントループ上で走らせるため封じる — review-2 M001)。
  シークレット新版の反映は **API 再起動**で行う(get_auth が再構築。scenario-3 で実証)。
"""

import base64
import json
import logging
import os
import threading
import time

import oci
from oci_genai_auth.auth import HttpxOciAuth

from .settings import get_settings

logger = logging.getLogger("jetuse.gen_shared_vault")

_REQUIRED = ("user", "tenancy", "fingerprint", "key_pem")
_FAIL_BACKOFF_S = 30.0  # 失敗の負キャッシュ窓(障害時のフェッチ嵐と loop 占有を防ぐ)
_NO_AUTO_REFRESH_S = 10**9  # 周期リフレッシュ(同期 I/O)を実質封じる refresh_interval


def _secrets_client():
    """genai._signer と同じ選択則: デプロイ = RP、それ以外(検証用)は ~/.oci DEFAULT。
    region は自環境(Vault は jetuse:dev と同リージョン)— db.py 等と同じ明示指定の流儀。"""
    if os.environ.get("AUTH_MODE") == "resource_principal":
        return oci.secrets.SecretsClient(
            {"region": get_settings().oci_region},
            signer=oci.auth.signers.get_resource_principals_signer())
    return oci.secrets.SecretsClient(oci.config.from_file())


def _fetch_material(secret_ocid: str) -> tuple[dict, int]:
    """CURRENT 版のシークレットを取得し (材料 dict, 版数) を返す。不備は ValueError。"""
    bundle = _secrets_client().get_secret_bundle(secret_ocid).data
    data = json.loads(base64.b64decode(bundle.secret_bundle_content.content))
    if not isinstance(data, dict) or not all(data.get(k) for k in _REQUIRED):
        raise ValueError("gen-shared secret JSON is missing required keys")
    return data, bundle.version_number


class GenSharedVaultAuth(HttpxOciAuth):
    """Vault 材料から組む user principal 署名(鍵は in-memory のみ)。

    材料の取得は構築時の 1 回だけ(get_auth 内 = off-loop)。周期/401 の再取得経路は
    いずれも同期 Vault I/O を httpx の(async クライアントでは)イベントループ上で走らせ、
    かつ失敗しても last_refresh が進まず要求ごとに再試行される(review-2 M001)。user
    principal は期限切れしないため自動再取得は不要 — refresh_interval を実質無限大にして
    周期経路を封じ、_refresh_signer(401 経路)は no-op にする。反映は API 再起動。"""

    def __init__(self, secret_ocid: str):
        material, version = _fetch_material(secret_ocid)
        # 値は出さない(鍵材料)。版数だけ観測可能にする(ローテーションの証跡)
        logger.info("loaded gen-shared signing material from vault (version %s)", version)
        signer = oci.signer.Signer(
            tenancy=material["tenancy"],
            user=material["user"],
            fingerprint=material["fingerprint"],
            private_key_file_location=None,
            private_key_content=material["key_pem"],
        )
        super().__init__(signer=signer, refresh_interval=_NO_AUTO_REFRESH_S)

    def _refresh_signer(self) -> None:
        # 401 経路でも Vault I/O をイベントループ上で走らせない(review-2 M001)。
        # 材料の張り替えは API 再起動で反映する(get_auth の再構築)。署名は据え置き。
        pass


_auth: GenSharedVaultAuth | None = None
_fail_until: float = 0.0
_lock = threading.Lock()  # コールドスタートの並行フェッチを single-flight 化(review-2 m002)


def get_auth() -> GenSharedVaultAuth | None:
    """成功はプロセス生涯キャッシュ。失敗は None(fail-closed)+ 短いバックオフ。

    ブロッキング呼び出し(SDK 同期 I/O)— async 経路からは asyncio.to_thread で呼ぶこと。
    並行呼び出しはロックで直列化し Vault フェッチを 1 回に束ねる(フェッチ嵐防止)。
    """
    global _auth, _fail_until
    secret_ocid = get_settings().gen_shared_secret_ocid
    if not secret_ocid:
        return None
    if _auth is not None:  # ロック外の高速路(成功後は生涯キャッシュ)
        return _auth
    with _lock:
        if _auth is not None:  # ロック待ちの間に別スレッドが構築済み(double-checked)
            return _auth
        if time.monotonic() < _fail_until:
            return None  # バックオフ中はフェッチしない(直近失敗の負キャッシュ)
        try:
            _auth = GenSharedVaultAuth(secret_ocid)
        except Exception:
            _fail_until = time.monotonic() + _FAIL_BACKOFF_S
            # 例外メッセージは OCI エラー情報のみ(シークレット内容を含まない)
            logger.warning(
                "failed to load gen-shared material from vault — "
                "shared models disabled (fail-closed)", exc_info=True)
            return None
    return _auth
