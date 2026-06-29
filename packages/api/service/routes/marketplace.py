"""マーケットプレイス UI 向けルート(PLG-06)。

アプリ内マーケット(`/marketplace`)のための薄い API 層。中央レジストリ(PLG-03 の
RegistryClient = list/get/download)のカタログ閲覧と、署名検証付きスナップショット取込
(PLG-03 installer.install/uninstall)を公開する。レジストリ通信・署名検証・取込の実体は
jetuse_core.plugins に閉じ、本ルートは次の 2 点のみを担う(spec-driven: specs/16-platform.md §6):

  1. カタログ(レジストリの配布一覧)に「インストール状態 / 更新有無(版比較)」を合成する。
  2. レジストリ/取込側の例外を予測可能な HTTP ステータスへ正規化する。

テスト容易性: RegistryClient の生成は `build_client()` に集約し、テストはこの関数と
`installer`/`store`(モジュール)を差し替えて実 DB・実レジストリ無しで検証する
(routes/usecases.py が repo モジュールを monkeypatch するのと同じ流儀)。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from jetuse_core.auth import AuthContext, require_user
from jetuse_core.plugins import installer, store
from jetuse_core.plugins.central_registry import CentralRegistryClient
from jetuse_core.plugins.installer import (
    AlreadyInstalled,
    IngestError,
    SignatureRejected,
)
from jetuse_core.plugins.manifest import ManifestError
from jetuse_core.plugins.registry_client import RegistryError, _semver_key
from jetuse_core.settings import Settings, get_settings
from service.deps import is_admin

#: install/uninstall に **運用者（ADMIN_USERS）ゲート**が要る kind（BE06-AUTHZ-001）。external-app
#: は全利用者へ SSO 起動導線（外部 URL）を platform-wide に露出するため、任意の認証利用者ではなく
#: 運用者だけが install/uninstall できる（他 kind は従来どおりセルフサービス）。
_ADMIN_ONLY_KINDS = frozenset({"external-app"})

router = APIRouter()

# version 序列はレジストリ側の最新版解決(client.get)と同じ規則を使う。表示用の版比較が
# client.get と食い違わないよう、登録済みの semver キー関数を単一の真実源として再利用する。
_vkey = _semver_key

# マーケット UI から install できる kind(installer がスナップショット取込できるもの)。
# MKT-01 で sample-app(scaffold 取込)/ connector(connector_store 登録)へ拡張した。installer は
# kind に応じた取込先へ署名検証付き・版固定・出所付きで取り込む(installer._ingest_contributes)。
# 未対応 kind は installable=False で表し、UI は install ボタンを無効化して未対応を明示する。
# external-app(ASSET-01 / BE-06)は §14.4 で後段としていた store+migration(external_app_instances /
# 026)＋installer の kind 分岐を実装し、本タスクで install 対応にした(署名検証・版固定・出所追跡は
# kind 非依存の枠組みをそのまま使う)。実シークレットは保存せず参照名のみ・SSO/Vault は人間ゲート。
SUPPORTED_KINDS = frozenset({"usecase", "agent", "sample-app", "connector", "external-app"})


# --- リクエスト DTO --------------------------------------------------------


class InstallRequest(BaseModel):
    plugin_id: str = Field(min_length=1)
    version: str | None = None


class UninstallRequest(BaseModel):
    plugin_id: str = Field(min_length=1)
    version: str = Field(min_length=1)


# --- レジストリクライアントの組み立て(テストで差し替え可能) ----------------


def build_client(settings: Settings) -> CentralRegistryClient:
    """設定の plugin_registry_url から PLG-04 形状の読取専用クライアントを組み立てる。

    レジストリ未設定(空 URL)は機能無効として 503 に倒す。
    """
    if not settings.plugin_registry_url:
        raise HTTPException(
            status_code=503,
            detail="プラグインレジストリが未設定です（plugin_registry_url）",
        )
    return CentralRegistryClient(base_url=settings.plugin_registry_url)


# --- カタログ合成(純粋関数。単体テスト対象) -------------------------------


def build_catalog(
    available: list[dict[str, Any]],
    installs: list[dict[str, Any]],
    *,
    viewer: str | None = None,
) -> list[dict[str, Any]]:
    """レジストリ配布一覧 × インストール記録 を plugin_id 単位のカード一覧へ合成する。

    `installed_plugins` は (plugin_id, version) 一意 = インスタンス単位の共有カタログ
    (PLG-02)。よって `installed`/`update_available` はインスタンス共通の事実として出す。
    一方、取込定義の削除(uninstall)は取り込んだ本人だけに許す(下の uninstall ゲート)ため、
    `can_uninstall` は viewer がその版の取込者(installed_by)である版があるときだけ True にする。

    - 同一 plugin_id の複数版はカード 1 枚にまとめ、最新版(semver)を代表に出す。
    - `installed_versions`: インストール済みの版(新しい順)。
    - `update_available`: 最新版(semver)が導入済み最大版より新しいときだけ True
      (downgrade / 旧版が現行 index から消えた場合に誤って更新ありにしない)。
    - `installable`: この kind を install ボタンから取込できるか(SUPPORTED_KINDS)。
    - `can_uninstall`: viewer が取込者である版があるか(uninstall を出してよいか)。
    """
    by_id: dict[str, list[dict[str, Any]]] = {}
    for e in available:
        pid = e.get("id")
        if not pid:
            continue
        by_id.setdefault(pid, []).append(e)

    # plugin_id -> {version -> installed_by}(版ごとの取込者を保持して所有判定に使う)。
    installed: dict[str, dict[str, str | None]] = {}
    for rec in installs:
        pid = rec.get("plugin_id")
        ver = rec.get("version")
        if pid and ver:
            installed.setdefault(pid, {})[ver] = rec.get("installed_by")

    cards: list[dict[str, Any]] = []
    for pid, entries in by_id.items():
        latest = max(entries, key=lambda e: _vkey(str(e.get("version", "0.0.0"))))
        inst_map = installed.get(pid, {})
        inst_versions = sorted(inst_map, key=lambda v: _vkey(v), reverse=True)
        is_installed = bool(inst_versions)
        latest_version = str(latest.get("version", ""))
        max_installed = inst_versions[0] if inst_versions else None
        update_available = is_installed and _vkey(latest_version) > _vkey(max_installed)
        # viewer が取込者である版だけ uninstall を許す。UI はこの版を送る(他人の版を送って
        # 404 になる多版・多所有者ケースを防ぐ)。viewer 未指定(純粋テスト)は全版を許可扱い。
        if viewer is not None:
            uninstallable = sorted(
                (v for v, by in inst_map.items() if by == viewer),
                key=lambda v: _vkey(v),
                reverse=True,
            )
        else:
            uninstallable = inst_versions
        can_uninstall = bool(uninstallable)
        cards.append(
            {
                "id": pid,
                "version": latest_version,
                "kind": latest.get("kind"),
                "name": latest.get("name") or pid,
                "description": latest.get("description") or "",
                "publisher": latest.get("publisher"),
                "tags": list(latest.get("tags") or []),
                "icon": latest.get("icon"),
                "versions": sorted(
                    {str(e.get("version", "")) for e in entries},
                    key=lambda v: _vkey(v),
                    reverse=True,
                ),
                "installed": is_installed,
                "installed_versions": inst_versions,
                "uninstallable_versions": uninstallable,
                "update_available": update_available,
                "installable": latest.get("kind") in SUPPORTED_KINDS,
                "can_uninstall": can_uninstall,
            }
        )
    cards.sort(key=lambda c: str(c["name"]).lower())
    return cards


def filter_catalog(
    cards: list[dict[str, Any]],
    *,
    q: str | None = None,
    tag: str | None = None,
    kind: str | None = None,
) -> list[dict[str, Any]]:
    """カタログを q(id/name/description 部分一致, 大小無視)・tag・kind で絞り込む。"""
    needle = (q or "").strip().lower()
    out = []
    for c in cards:
        if kind and c.get("kind") != kind:
            continue
        if tag and tag not in (c.get("tags") or []):
            continue
        if needle:
            hay = " ".join(
                str(c.get(k) or "") for k in ("id", "name", "description")
            ).lower()
            if needle not in hay:
                continue
        out.append(c)
    return out


# --- ルート ----------------------------------------------------------------


@router.get("/api/marketplace/plugins")
def list_marketplace(
    user: Annotated[AuthContext, Depends(require_user)],
    settings: Annotated[Settings, Depends(get_settings)],
    q: str | None = None,
    tag: str | None = None,
    kind: str | None = None,
):
    """配布カタログ(インストール状態・更新有無つき)を一覧する。"""
    client = build_client(settings)
    try:
        available = client.list()
    except RegistryError as e:
        raise HTTPException(status_code=502, detail=f"レジストリ取得に失敗: {e}") from e
    cards = build_catalog(available, store.list_installs(), viewer=user.subject)
    cards = filter_catalog(cards, q=q, tag=tag, kind=kind)
    tags = sorted({t for c in cards for t in (c.get("tags") or [])})
    return {"plugins": cards, "tags": tags}


@router.get("/api/marketplace/plugins/{namespace}/{name}")
def get_marketplace_plugin(
    namespace: str,
    name: str,
    user: Annotated[AuthContext, Depends(require_user)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    """プラグイン 1 件の詳細(最新 manifest 全文 + 版一覧 + インストール状態)を返す。

    plugin_id は `namespace/name`(manifest の id 規約)なのでパスを 2 セグメントで受ける。
    """
    plugin_id = f"{namespace}/{name}"
    client = build_client(settings)
    try:
        entries = [e for e in client.list() if e.get("id") == plugin_id]
    except RegistryError as e:
        raise HTTPException(status_code=502, detail=f"レジストリ取得に失敗: {e}") from e
    if not entries:
        raise HTTPException(status_code=404, detail=f"未登録のプラグイン: {plugin_id}")
    try:
        manifest = client.download(plugin_id)  # version=None → 最新版
    except (RegistryError, ManifestError) as e:
        raise HTTPException(status_code=502, detail=f"詳細の取得に失敗: {e}") from e

    installs = store.list_installs(plugin_id)
    cards = build_catalog(entries, installs, viewer=user.subject)
    card = cards[0] if cards else {}
    md = manifest.model_dump(by_alias=True)
    # 詳細でだけ見せる重い項目(permissions / requires / 署名有無)を載せる。
    card.update(
        {
            "name": md.get("name") or card.get("name"),
            "description": md.get("description") or card.get("description"),
            "permissions": md.get("permissions", []),
            "requires": md.get("requires", {}),
            "license": md.get("license"),
            "signed": manifest.signature is not None,
        }
    )
    return card


@router.post("/api/marketplace/install")
def install_plugin(
    req: InstallRequest,
    user: Annotated[AuthContext, Depends(require_user)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    """レジストリから取得・署名検証してスナップショット取込する(PLG-03 / MKT-01 install)。

    取込結果は kind に応じた取込先に出現する: usecase/agent はホーム(/api/usecases・/api/agents)、
    sample-app は scaffold(sample_app_instances)、connector は登録(connector_instances)。
    """
    client = build_client(settings)

    # external-app は SSO 起動導線を全利用者へ露出するため、運用者（ADMIN_USERS）ゲート
    # をかける（BE06-AUTHZ-001）。**取込と同一の署名検証済み manifest** に対して認可フックで強制し、
    # 二重 download による TOCTOU を避ける（BE06-BLK-004）。他 kind は従来どおりセルフサービス。
    def _authorize(manifest):
        if manifest.kind in _ADMIN_ONLY_KINDS and not is_admin(user):
            raise HTTPException(
                status_code=403, detail=f"{manifest.kind} の install は運用者(ADMIN_USERS)のみ許可"
            )

    try:
        record = installer.install(
            client,
            req.plugin_id,
            req.version,
            installed_by=user.subject,
            owner=user.subject,
            authorize=_authorize,
        )
    except AlreadyInstalled as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except SignatureRejected as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except (IngestError, ManifestError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RegistryError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return {
        "installed": True,
        "plugin_id": record.get("plugin_id"),
        "version": record.get("version"),
        "kind": record.get("kind"),
        "ingested": record.get("ingested", []),
    }


@router.post("/api/marketplace/uninstall")
def uninstall_plugin(
    req: UninstallRequest,
    user: Annotated[AuthContext, Depends(require_user)],
):
    """取込んだ定義を除去しインストール記録を削除する(PLG-03 uninstall)。

    所有者ゲート: installer.uninstall は (plugin_id, version) で取込定義を一括削除する
    (出所キー削除)。任意のログインユーザーが他人の取込定義を消せないよう、取込んだ本人
    (installed_plugins.installed_by == user.subject)以外には実行させない。第三者には存在を
    伏せて 404 を返す(未インストールと区別しない)。
    """
    record = store.find_install(req.plugin_id, req.version)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"未インストール: {req.plugin_id}@{req.version}",
        )
    if record.get("kind") in _ADMIN_ONLY_KINDS:
        # external-app は platform-wide な運用者管理資産。**任意の現行運用者**が uninstall できる
        # （原 installer の離任・無効化に依存しない。BE06-MAJ-004）。非運用者には伏せて 404。
        if not is_admin(user):
            raise HTTPException(
                status_code=404,
                detail=f"未インストール: {req.plugin_id}@{req.version}",
            )
    elif record.get("installed_by") != user.subject:
        # 従来 kind: 取込本人のみ（第三者には存在を伏せて 404）。
        raise HTTPException(
            status_code=404,
            detail=f"未インストール: {req.plugin_id}@{req.version}",
        )
    removed = installer.uninstall(req.plugin_id, req.version)
    if not removed:
        raise HTTPException(
            status_code=404,
            detail=f"未インストール: {req.plugin_id}@{req.version}",
        )
    return {"uninstalled": True, "plugin_id": req.plugin_id, "version": req.version}
