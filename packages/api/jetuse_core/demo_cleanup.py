"""DELETE /api/demos/{id} の後始末オーケストレーション(specs/18 §3.2 手順 1〜5)。

- 各ステップは冪等(NotFound / ORA-00942 は成功扱い)で、途中失敗しても再 DELETE で収束する。
- 掃除の列挙は exact な根拠に限る: 表 = registry(exact owner)、store = CP 一覧の
  metadata exact 一致、file = ledger 行 + file_key 接頭辞、原本 = 完全 namespace prefix、
  profile/index = 各バックエンド命名関数の決定的名前。短縮 hash の接頭辞走査は使わない。
- スキップ判定の正は台帳(demo_backend_targets): 記録があるのに接続設定が構成できなければ
  スキップせず 503(demo 行を保持 — 設定復旧後の再 DELETE で収束)。
- 失敗時の応答: その段階を detail に含む 503(status='deleting' の行が残る)。
- リースは手順 1 で取得し、手順 5 の demos 行削除 commit まで保持する(DELETE 同士も直列化)。
"""

import logging
from collections.abc import Callable

from openai import NotFoundError

from . import conversations, datasets, demo_lease, demo_targets, demos, rag, rag_ledger
from .db import connect
from .genai import (
    make_cp_client,
    make_cp_client_for,
    make_inference_client,
    make_inference_client_for,
)
from .owner_keys import owner_hash, owner_key_gate

logger = logging.getLogger("jetuse.demo_cleanup")


class DemoNotFoundError(Exception):
    """行なし/非所有(存在秘匿の同一 404)。後着 DELETE の「先着成功」もこれ(=404)。"""


class CleanupError(Exception):
    """後始末の途中失敗。stage を detail に含む 503 → 再 DELETE で収束。"""

    def __init__(self, stage: str, cause: Exception):
        self.stage = stage
        super().__init__(f"demo delete failed at {stage}: {str(cause)[:300]}")


def _stage(name: str, fn: Callable[[], None]) -> None:
    try:
        fn()
    except CleanupError:
        raise
    except Exception as e:
        logger.exception("demo cleanup stage failed: %s", name)
        raise CleanupError(name, e) from e


def delete_demo_box(demo_id: str, owner_sub: str) -> dict:
    """所有者の DELETE(specs/18 §2.1)。deleting 状態の残骸にも受理(再実行 = 収束)。"""
    owner_key_gate()
    d = demos.get_demo(demo_id)
    if not d or d["owner_sub"] != owner_sub:
        raise DemoNotFoundError(demo_id)
    with demo_lease.acquire(demo_id):
        # リース下で再確認(DELETE 取得 = allow_deleting: deleting は受理して後始末を再開)
        d = demos.get_demo(demo_id)
        if not d or d["owner_sub"] != owner_sub:
            raise DemoNotFoundError(demo_id)  # 先着 DELETE 成功後の後着 = 404
        ns = f"demo_{demo_id}"
        if d["status"] != "deleting":
            demos.set_status(demo_id, d["status"], "deleting")  # 遷移 + commit(手順 1)
        _stage("datasets", lambda: datasets.delete_owner(ns))            # 手順 2
        _cleanup_rag(ns)                                                 # 手順 3
        _stage("conversations",
               lambda: conversations.delete_demo_conversations(demo_id))  # 手順 4
        _stage("demo-row", lambda: demos.delete_demo(owner_sub, demo_id))  # 手順 5
    return {"deleted": True}


# --- 手順 3: RAG 箱(全バックエンド) ---


def _dp_clients(ns: str) -> list:
    """DP(Files 系)クライアント: 台帳 kind='files' の全 locator を正とする。記録がある
    のに locator が構成できない場合は例外(スキップせず 503 — 台帳が正)。記録が無い legacy
    のときだけ現在設定へフォールバック(構成ドリフトで現在設定が空/無効でも旧 File に到達する
    ため、現在設定を無条件に先頭へ置かない — codex review-8 B002)。"""
    clients = []
    seen = set()
    for t in demo_targets.targets_for(ns, "files"):
        loc = t["locator"]
        key = (loc.get("region"), loc.get("compartment"), loc.get("project"))
        if not all(key):
            raise RuntimeError(f"files target locator incomplete: {loc}")
        if key in seen:
            continue
        seen.add(key)
        clients.append(make_inference_client_for(*key))
    if not clients:
        clients.append(make_inference_client(with_project=True))
    return clients


def _cp_clients(ns: str) -> list:
    """CP(store 本体)クライアント: 台帳 kind='vector_store' の全 locator を正とし、記録が
    無い legacy のときだけ現在設定へフォールバック(理由は _dp_clients と同じ — B002)。"""
    clients = []
    seen = set()
    for t in demo_targets.targets_for(ns, "vector_store"):
        loc = t["locator"]
        key = (loc.get("region"), loc.get("compartment"))
        if not all(key):
            raise RuntimeError(f"vector_store target locator incomplete: {loc}")
        if key in seen:
            continue
        seen.add(key)
        clients.append(make_cp_client_for(*key))
    if not clients:
        clients.append(make_cp_client())
    return clients


def _cleanup_rag(ns: str) -> None:
    tag = owner_hash(ns)

    # 3a: rag_files の namespace 行を列挙 → DP 削除(NotFound 成功)→ 行削除 → ledger 解放
    def step_files() -> None:
        vs_id = rag.get_store_id(ns)
        for row in rag.list_files(ns):
            # 行ごとの write-ahead locator で client を構成する(現在設定固定だと構成ドリフト後に
            # 空/無効な現在設定で停止し、台帳が持つ旧 project の File に到達できない — B002)。
            led = rag_ledger.rows_for_owner_by_id(row["id"])
            dp = rag._dp_for((led or {}).get("locator") or None)
            if vs_id:
                try:
                    dp.vector_stores.files.delete(
                        vector_store_id=vs_id, file_id=row["oci_file_id"]
                    )
                except NotFoundError:
                    pass
            rag.delete_external_file(row["oci_file_id"], dp)
            with connect() as conn:
                conn.cursor().execute(
                    "DELETE FROM rag_files WHERE id = :id AND owner_sub = :o",
                    id=row["id"], o=ns,
                )
                conn.commit()
            rag_ledger.release(row["id"])

    _stage("rag-files", step_files)

    # 3b: store 本体 — rag_stores 行の ID + CP 一覧(全 locator・ページネーション完走)から
    # metadata.owner == sha1(ns) を列挙して削除。失敗は中断(503 — 枠漏れ防止)。
    # rag_stores 行と台帳行の削除は 3f まで全て成功した後(最後)に行う。
    def step_store() -> None:
        # store は locator ごとに存在しうる(構成変更で region/compartment が変わる)。
        # 発見元 locator の対応を保持し、その client で削除する。現在設定の NotFound で
        # 打ち切ると旧 locator の実 store を消し逃す(codex review-2 blocker):
        # NotFound は「その locator では不存在」に過ぎず、全 locator で実削除 or 不存在を
        # 確認して初めて成功。
        clients = _cp_clients(ns)
        pairs: list[tuple] = []  # (store_id, discovering_client)
        registered = rag.get_store_id(ns)
        for cp in clients:
            found = {vs.id for vs in rag.find_orphan_stores(ns, cp)}
            for sid in found:
                pairs.append((sid, cp))
            # 登録行の ID は「全」 locator で削除を試す(旧 locator の legacy/metadata 無し
            # store は find_orphan_stores の metadata 照合に掛からず、単一 client では
            # NotFound で消し逃す — 事後確認も metadata 依存なので検出できない。M005)。
            if registered and registered not in found:
                pairs.append((registered, cp))
        seen: set[str] = set()
        for vs_id, cp in pairs:
            if vs_id in seen:
                continue
            try:
                cp.vector_stores.delete(vector_store_id=vs_id)
                seen.add(vs_id)  # 実削除成功
            except NotFoundError:
                # この locator では不存在。他 client が同 ID を持てばそちらで削除される
                continue
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(f"store delete failed ({vs_id}): {e}") from e
        # 事後確認: 全 client のページネーション完走で当該 owner の store が残っていない
        for cp in clients:
            leftover = rag.find_orphan_stores(ns, cp)
            if leftover:
                raise RuntimeError(
                    f"stores still present after delete: {[s.id for s in leftover]}")

    _stage("rag-store", step_store)

    # 3c: ledger の owner 全行(pending/confirmed)を解放 + DP files 一覧の接頭辞孤児を削除
    def step_ledger_and_orphans() -> None:
        dps = _dp_clients(ns)
        for row in rag_ledger.rows_for_owner(ns):
            if row["external_file_id"]:
                for dp in dps:
                    rag.delete_external_file(row["external_file_id"], dp)
            rag.delete_original_exact(ns, row["id"], row["ext"],
                                      locator=row.get("locator") or None)
            rag_ledger.release(row["id"])
        prefix = f"{tag}/"
        for dp in dps:
            for f in rag.list_all_external_files(dp):
                if f["filename"].startswith(prefix):
                    rag.delete_external_file(f["id"], dp)
        # 事後条件: 当該 owner の ledger 行がゼロ
        if rag_ledger.rows_for_owner(ns):
            raise RuntimeError("ledger rows remain after cleanup")

    _stage("rag-ledger", step_ledger_and_orphans)

    # 3d: Select AI profile/vector index(不存在は無視。命名は rag_select_ai が正)
    def step_select_ai() -> None:
        from . import rag_select_ai

        rag_select_ai.delete_owner(ns)

    _stage("rag-select-ai", step_select_ai)

    # 3e: OpenSearch — 台帳が正: 記録があれば設定の有効/無効にかかわらず保存 locator で削除
    def step_opensearch() -> None:
        from . import rag_opensearch

        targets = demo_targets.targets_for(ns, "opensearch")
        for t in targets:
            endpoint = t["locator"].get("endpoint")
            if not endpoint:
                raise RuntimeError(f"opensearch target without endpoint: {t['locator']}")
            rag_opensearch.delete_owner(ns, endpoint=endpoint)

    _stage("rag-opensearch", step_opensearch)

    # 3f: Object Storage 原本 prefix の全列挙削除(台帳が正・versioning 無効を確認)
    def step_originals() -> None:
        targets = demo_targets.targets_for(ns, "objectstorage")
        for t in targets:
            loc = t["locator"]
            if not loc.get("bucket"):
                raise RuntimeError(f"objectstorage target without bucket: {loc}")
            versioning = rag.bucket_versioning(loc)
            if versioning not in (None, "Disabled"):
                # Suspended でも既存 version が残る(M004)— 「残骸なし」が偽陽性になるため停止
                raise RuntimeError(
                    f"bucket versioning is {versioning} (must be Disabled) — human action"
                )
            rag.delete_objects(rag.list_original_objects(ns, loc), loc)

    _stage("rag-originals", step_originals)

    # 3 完了後: rag_stores 行と台帳行の削除(途中失敗の再 DELETE が旧 locator を参照できる
    # よう最後まで保持する — specs/18 §3.2 手順 3b)
    def step_rows() -> None:
        with connect() as conn:
            conn.cursor().execute("DELETE FROM rag_stores WHERE owner_sub = :o", o=ns)
            conn.commit()
        demo_targets.delete_targets(ns)

    _stage("rag-rows", step_rows)
