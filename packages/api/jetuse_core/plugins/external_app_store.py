"""external-app の登録（インスタンスへの取込）ロジック（ASSET-01 / BE-06）。

`kind: external-app` の manifest を受け取り、定義（`ExternalAppDefinition`）の構造検証を通して、
配布表現（embed/url/title/sso）を **インスタンス側 ADB へ登録**する。これにより取込後は
`external_app_instances`（定義 CLOB）が ADB に出現し、マーケット install（installer / MKT-01）で
external-app をオンボードできる（§14.4 で後段としていた store＋migration を本タスクで実装）。

責務境界（connector_store.py と同じ規約）:
  - 定義の構造検証は `external_app.py`（validate_external_app）。本モジュールは確認後に登録する。
  - **実シークレット値（client_secret / id_token）は保存しない**。定義 CLOB に含まれるのは
    `clientIdRef`/`secretRef`（実値ではなく論理参照名）のみ（spec §14.2・§12.2 の機密区分）。実値は
    install 時に Vault(OCID) へ束ねる（人間ゲート）。本テーブルは秘密値の列を持たない。
  - 出所追跡（ADR-0013 D6）: plugin_id / source_version は manifest 由来（installed_plugins 対応）。
    uninstall は出所キーで一括削除する（delete_by_source）。

store.py / connector_store.py と同じ規約: connect() で接続し、関数内で commit する単一表の原子操作。
manifest は配布表現（camelCase, model_dump(by_alias=True)）のまま CLOB に格納する。
"""

import json
import uuid
from typing import Any

from ..db import connect
from .external_app import ExternalAppDefinition, validate_external_app
from .manifest import PluginManifest

# DB カラム幅（VARCHAR2）と一致させる入力上限（connector_store.py と同方針）。
MAX_REGISTERED_BY_LEN = 255
MAX_NAME_LEN = 200

_INSTANCE_COLS = (
    "id, plugin_id, source_version, name, app, embed, "
    "definition, registered_by, created_at"
)


def _uid() -> str:
    return str(uuid.uuid4())


def _read_clob(raw: Any) -> Any:
    if raw is not None and hasattr(raw, "read"):
        raw = raw.read()
    return raw


def _instance_row_to_record(r) -> dict[str, Any]:
    created_at = r[8]
    definition: dict | None
    definition_error = False
    raw = _read_clob(r[6])
    if raw:
        try:
            definition = json.loads(raw)
        except (ValueError, TypeError):
            definition = None
            definition_error = True
    else:
        definition = None
    return {
        "id": r[0],
        "plugin_id": r[1],
        "source_version": r[2],
        "name": r[3],
        "app": r[4],
        "embed": r[5],
        "definition": definition,
        "definition_error": definition_error,
        "registered_by": r[7],
        "created_at": created_at.isoformat()
        if hasattr(created_at, "isoformat")
        else created_at,
    }


def register_external_app(
    manifest: PluginManifest,
    *,
    registered_by: str,
    name: str | None = None,
) -> dict[str, Any]:
    """external-app manifest をインスタンスへ登録し、登録後のインスタンス記録を返す。

    手順:
      1. 定義（contributes["external-app"]）を構造検証する（validate_external_app）。
      2. インスタンス行（定義 CLOB）を 1 トランザクションで挿入する。

    **実シークレット値は保存しない**（定義に含まれるのは clientIdRef/secretRef = 参照名のみ）。
    """
    if manifest.kind != "external-app":
        raise ValueError(
            f"register できるのは kind=external-app の manifest のみ: {manifest.kind}"
        )
    if not registered_by or not registered_by.strip():
        raise ValueError("registered_by は非空でなければならない")
    if len(registered_by) > MAX_REGISTERED_BY_LEN:
        raise ValueError(
            f"registered_by は {MAX_REGISTERED_BY_LEN} 文字以内でなければならない"
        )

    definition: ExternalAppDefinition = validate_external_app(manifest)

    # name 未指定（None）のときだけ manifest.name にフォールバックする。明示的に空/空白を渡したら
    # サイレントにフォールバックせずエラーにする（connector_store.py と同じ）。
    inst_name = (manifest.name if name is None else name).strip()
    if not inst_name:
        raise ValueError("name は非空でなければならない")
    if len(inst_name) > MAX_NAME_LEN:
        raise ValueError(f"name は {MAX_NAME_LEN} 文字以内でなければならない")

    inst_id = _uid()
    # 定義は配布表現（camelCase: clientIdRef/secretRef）で保存し、取り出し時に往復できるようにする。
    definition_json = json.dumps(
        definition.model_dump(by_alias=True), ensure_ascii=False
    )

    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            INSERT INTO external_app_instances(
              {_INSTANCE_COLS.replace(', created_at', '')})
            VALUES (:id, :pid, :ver, :name, :app, :embed, :defn, :registrar)
            """,
            id=inst_id,
            pid=manifest.id,
            ver=manifest.version,
            name=inst_name,
            app=definition.app,
            embed=definition.embed,
            defn=definition_json,
            registrar=registered_by,
        )
        conn.commit()
        cur.execute(
            f"SELECT {_INSTANCE_COLS} FROM external_app_instances WHERE id = :id",
            id=inst_id,
        )
        rec = _instance_row_to_record(cur.fetchone())

    return rec


def get_external_app(instance_id: str) -> dict[str, Any] | None:
    """登録済み external-app を id で取得する。無ければ None。"""
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT {_INSTANCE_COLS} FROM external_app_instances WHERE id = :id",
            id=instance_id,
        )
        row = cur.fetchone()
        return _instance_row_to_record(row) if row else None


def list_external_apps(
    plugin_id: str | None = None,
    app: str | None = None,
    registered_by: str | None = None,
) -> list[dict[str, Any]]:
    """登録済み external-app を新しい順に一覧する。plugin_id / app / registered_by で絞り込める。

    install は platform-wide（署名検証済み・運用者ゲート。版全体一意）なので起動導線は全体可視で扱う
    （registered_by は管理用途の任意フィルタであって分離保証ではない。BE06-REV-005）。
    """
    where = []
    binds: dict[str, Any] = {}
    if plugin_id is not None:
        where.append("plugin_id = :pid")
        binds["pid"] = plugin_id
    if app is not None:
        where.append("app = :app")
        binds["app"] = app
    if registered_by is not None:
        where.append("registered_by = :rb")
        binds["rb"] = registered_by
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT {_INSTANCE_COLS} FROM external_app_instances{clause}
            ORDER BY created_at DESC FETCH FIRST 500 ROWS ONLY
            """,
            **binds,
        )
        return [_instance_row_to_record(r) for r in cur.fetchall()]


def remove_external_app(instance_id: str) -> bool:
    """登録済み external-app を削除する。削除した行があれば True。"""
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM external_app_instances WHERE id = :id", id=instance_id
        )
        removed = cur.rowcount
        conn.commit()
    return removed > 0


def delete_by_source(plugin_id: str, version: str) -> int:
    """出所（plugin_id, source_version）に一致する external-app インスタンスを全削除する。

    マーケット取込（installer / MKT-01）の uninstall で使う。削除した件数を返す
    （冪等: 対象が無ければ 0）。
    """
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            DELETE FROM external_app_instances
            WHERE plugin_id = :pid AND source_version = :ver
            """,
            pid=plugin_id,
            ver=version,
        )
        deleted = cur.rowcount
        conn.commit()
    return deleted
