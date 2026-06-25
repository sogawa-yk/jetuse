"""スナップショット取込 / アンインストール(PLG-03 / D6・D7)。

中央レジストリ(registry_client.py)から取得した manifest を、発行者の ed25519 署名で
検証(D7)したうえで、`contributes` を **版固定** で ADB へ書き込む(D6 スナップショット取込)。
取り込んだ定義には出所(`source_plugin_id`/`source_version`)を刻み、アンインストールで
出所キーごと除去できるようにする。

責務分担:
  - registry_client.RegistryClient: list/get/download + 公開鍵取得(通信)。
  - manifest.verify_signature: ed25519 署名検証(真正性。fail-closed)。
  - usecases/agents.insert_ingested / delete_by_source: 取込定義の永続化(出所追跡)。
  - store(installed_plugins): インストール記録の CRUD(PLG-02)。

トランザクション境界(MVP の割り切り):
  各リポジトリ(usecases/agents/store)は内部で個別に commit する(既存規約)。本タスクでは
  単一接続での厳密な原子トランザクションには踏み込まず、「取込定義を先に書き、インストール記録を
  最後に書く。途中失敗時は書いた取込定義を出所キーで補償削除する」ことで実務上の整合を担保する
  (store.py の注記参照。完全な 1 トランザクション化は接続共有の再設計が必要で PLG-07 の範疇)。
"""

from __future__ import annotations

from typing import Any

from .. import agents, usecases
from . import store
from .manifest import PluginManifest, verify_signature
from .registry_client import RegistryError


class SignatureRejected(Exception):
    """署名が無い/検証に失敗した manifest の取込を拒否したときに送出する(D7)。"""


class IngestError(Exception):
    """contributes の取込に失敗したとき(未対応 kind 等)に送出する。"""


class AlreadyInstalled(Exception):
    """同一 (plugin_id, version) が既にインストール済みのときに送出する(版固定の二重取込防止)。"""


def _ingest_contributes(
    manifest: PluginManifest, owner: str, *, visibility: str
) -> list[tuple[str, str]]:
    """manifest.contributes を kind に応じて usecases/agents へ取り込み、(table, id) を返す。

    版固定: 書き込む定義に source_plugin_id=manifest.id / source_version=manifest.version を刻む。
    返り値は補償削除・呼び出し側の確認用(取込で作られた行の (テーブル名, id) リスト)。
    """
    kind = manifest.kind
    payload = dict(manifest.contributes[kind])
    # 表示メタは manifest トップレベルを既定にする(payload が持てば payload 優先)。
    payload.setdefault("name", manifest.name)
    payload.setdefault("description", manifest.description)
    if manifest.icon is not None:
        payload.setdefault("icon", manifest.icon)
    if manifest.tags:
        payload.setdefault("tags", list(manifest.tags))

    created: list[tuple[str, str]] = []
    if kind == "usecase":
        uc_id = usecases.insert_ingested(
            owner, payload,
            source_plugin_id=manifest.id, source_version=manifest.version,
            visibility=visibility,
        )
        created.append(("usecases", uc_id))
    elif kind == "agent":
        ag_id = agents.insert_ingested(
            owner, payload,
            source_plugin_id=manifest.id, source_version=manifest.version,
            visibility=visibility,
        )
        created.append(("agents", ag_id))
    else:  # manifest 検証で kind は usecase|agent に限定済み。防御的に拒否する。
        raise IngestError(f"取込に未対応の kind: {kind}")
    return created


def _delete_ingested(plugin_id: str, version: str) -> int:
    """取込定義(usecases/agents)を出所キーで全削除し、合計削除件数を返す(uninstall 用)。"""
    return (
        usecases.delete_by_source(plugin_id, version)
        + agents.delete_by_source(plugin_id, version)
    )


def _delete_created(created: list[tuple[str, str]]) -> None:
    """今回の取込で作成した (table, id) だけを補償削除する。

    出所キーでの一括削除(_delete_ingested)を使うと、同一 (plugin_id, version) で既に取り込まれた
    別レコードまで巻き込みかねない(record_install が一意制約違反で失敗した場合など)。補償は
    「いま作った行」に限定する(Codex F-001 / blocker への対応)。
    """
    for table, rid in created:
        if table == "usecases":
            usecases.delete_ingested(rid)
        elif table == "agents":
            agents.delete_ingested(rid)


def install(
    client,
    plugin_id: str,
    version: str | None = None,
    *,
    installed_by: str,
    owner: str | None = None,
    visibility: str = "private",
) -> dict[str, Any]:
    """レジストリからプラグインを取得・署名検証し、スナップショット取込する(D6/D7)。

    手順:
      1. client.download で manifest を取得・構文検証する。
      2. 署名(D7)を検証する。署名が無い/公開鍵取得失敗/検証失敗は SignatureRejected で拒否し、
         ADB には一切書き込まない(fail-closed)。
      3. 同一 (plugin_id, version) が既にインストール済みなら AlreadyInstalled で拒否する
         (版固定スナップショットの二重取込防止。取込前に確認するため ADB を汚さない)。
      4. contributes を版固定で usecases/agents に取り込む(source_* を刻む)。
      5. installed_plugins に記録する(signature_verified=True)。記録に失敗したら、
         「いま作った取込定義だけ」を補償削除して整合を保つ。

    返り値はインストール記録(store.record_install の戻り)に `ingested`
    ((table,id) の一覧)を加えた dict。`owner` 未指定なら installed_by を取込定義の所有者にする。
    """
    owner = owner or installed_by
    manifest = client.download(plugin_id, version)

    # --- 署名検証(D7 / fail-closed) ---
    sig = manifest.signature
    if sig is None:
        raise SignatureRejected(f"未署名 manifest は取込拒否: {manifest.id}@{manifest.version}")
    # 公開鍵取得は署名検証境界として扱う。未登録 publicKeyId・不正鍵(RegistryError)は
    # 「検証できない=取込不可」に正規化する(Codex F-002 / docstring の契約と一致)。
    try:
        public_key = client.public_key(sig.public_key_id)
    except RegistryError as e:
        raise SignatureRejected(
            f"発行者公開鍵を取得できず取込拒否: {manifest.id}@{manifest.version}: {e}"
        ) from e
    if not verify_signature(manifest, public_key):
        raise SignatureRejected(
            f"署名検証に失敗したため取込拒否: {manifest.id}@{manifest.version}"
        )

    # --- 二重取込の防止(取込前にチェックして ADB を汚さない) ---
    if store.find_install(manifest.id, manifest.version) is not None:
        raise AlreadyInstalled(f"既にインストール済み: {manifest.id}@{manifest.version}")

    # --- スナップショット取込 ---
    source_registry = getattr(client, "base_url", "") or None
    created = _ingest_contributes(manifest, owner, visibility=visibility)
    try:
        record = store.record_install(
            installed_by, manifest,
            source_registry=source_registry, signature_verified=True,
        )
    except Exception:
        # 記録に失敗したら、いま作った取込定義だけを補償削除する(既存の同版取込は巻き込まない)。
        _delete_created(created)
        raise
    record["ingested"] = created
    return record


def uninstall(plugin_id: str, version: str) -> bool:
    """取込んだ定義を除去し installed_plugins から記録を削除する。

    取込定義(usecases/agents の出所キー一致)を全削除し、対応するインストール記録も消す。
    記録が存在し削除できたら True、対象記録が無ければ False(取込定義の削除は冪等に試みる)。
    """
    _delete_ingested(plugin_id, version)
    record = store.find_install(plugin_id, version)
    if record is None:
        return False
    return store.delete_install(record["id"])
