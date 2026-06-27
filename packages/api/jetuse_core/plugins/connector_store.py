"""connector の登録(インスタンスへの取込)ロジック(CON-01)。

`kind: connector` の manifest を受け取り、合成バリデーション(宣言整合)を通したうえで、定義
(provider/transport/actions/auth)を **インスタンス側 ADB へ登録**する。これにより取込後は
`connector_instances`(定義 CLOB)が ADB に出現する。

責務境界:
  - 定義の構造検証・合成バリデーションは `connector.py`。本モジュールは ok を確認してから登録する。
  - 合成バリデーションが致命的不整合(権限スコープ宣言違反)を検出した場合は **DB に何も書かず**
    `ConnectorCompositionError` を送出する(fail-closed)。
  - **実シークレット値(トークン/パスワード)は保存しない**。定義 CLOB は発行 manifest の
    contributes["connector"] を配布表現のまま往復保存する。ここに含まれる secret_ref は
    **実値ではなく宣言の一部である論理参照名**(例 slack-bot-token = 非機密。spec §12.2)であり、
    保持してよい(install 時にどの Vault 秘密を束ねるか復元するため)。実シークレットは install 時に
    Vault(OCID)へ束ねる(CON-02/03)。本テーブルは秘密値の列を持たない。
  - レジストリ取込(installed_plugins への記録)は PLG-02/03 の責務。本モジュールは connector の
    登録のみを行う(installed_plugins とは plugin_id/source_version で対応づく)。

store.py / scaffold.py と同じ規約: connect() で接続し、関数内で commit する単一表の原子操作。
manifest は配布表現(camelCase, model_dump(by_alias=True))のまま CLOB に格納する。
"""

import json
import uuid
from typing import Any

from ..db import connect
from .connector import (
    ConnectorCompositionError,
    ConnectorDefinition,
    validate_connector,
    validate_connector_composition,
)
from .manifest import PluginManifest

# DB カラム幅(VARCHAR2)と一致させる入力上限。超えると保存時に ORA-12899 になるため、
# 書き込み境界で弾いて予測可能な ValueError にする(store.py / scaffold.py と同じ方針)。
MAX_REGISTERED_BY_LEN = 255
MAX_NAME_LEN = 200

_INSTANCE_COLS = (
    "id, plugin_id, source_version, name, provider, transport, "
    "definition, registered_by, created_at"
)


def _uid() -> str:
    return str(uuid.uuid4())


def _read_clob(raw: Any) -> Any:
    # CLOB は db 既定(fetch_lobs=False)で str。設定差で LOB が返る構成にも備える。
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
        "provider": r[4],
        "transport": r[5],
        "definition": definition,
        "definition_error": definition_error,
        "registered_by": r[7],
        "created_at": created_at.isoformat()
        if hasattr(created_at, "isoformat")
        else created_at,
    }


def register_connector(
    manifest: PluginManifest,
    *,
    registered_by: str,
    name: str | None = None,
) -> dict[str, Any]:
    """connector manifest をインスタンスへ登録し、登録後のインスタンス記録を返す。

    手順:
      1. 定義(contributes["connector"])を構造検証する。
      2. 合成バリデーション(権限スコープ宣言整合)を実行する。
         **致命的不整合があれば DB に何も書かず ConnectorCompositionError を送出**(fail-closed)。
      3. インスタンス行(定義 CLOB)を 1 トランザクションで挿入する。

    **認証の実値は保存しない**(定義に含まれるのは secret_ref = 参照名のみ)。
    """
    if manifest.kind != "connector":
        raise ValueError(
            f"register できるのは kind=connector の manifest のみ: {manifest.kind}"
        )
    if not registered_by or not registered_by.strip():
        raise ValueError("registered_by は非空でなければならない")
    if len(registered_by) > MAX_REGISTERED_BY_LEN:
        raise ValueError(
            f"registered_by は {MAX_REGISTERED_BY_LEN} 文字以内でなければならない"
        )

    definition: ConnectorDefinition = validate_connector(manifest)
    report = validate_connector_composition(manifest, definition=definition)
    if not report.ok:
        # fail-closed: 宣言整合違反を検出したら一切登録しない。
        raise ConnectorCompositionError(report)

    # name 未指定(None)のときだけ manifest.name にフォールバックする。明示的に空/空白を渡したら
    # サイレントにフォールバックせずエラーにする(呼び出し側の取り違えを検出。scaffold.py と同じ)。
    inst_name = (manifest.name if name is None else name).strip()
    if not inst_name:
        raise ValueError("name は非空でなければならない")
    if len(inst_name) > MAX_NAME_LEN:
        raise ValueError(f"name は {MAX_NAME_LEN} 文字以内でなければならない")

    inst_id = _uid()
    # 定義は配布表現(camelCase: secretRef)で保存し、取り出し時に往復できるようにする。
    definition_json = json.dumps(
        definition.model_dump(by_alias=True), ensure_ascii=False
    )

    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            INSERT INTO connector_instances(
              {_INSTANCE_COLS.replace(', created_at', '')})
            VALUES (:id, :pid, :ver, :name, :prov, :trans, :defn, :registrar)
            """,
            id=inst_id,
            pid=manifest.id,
            ver=manifest.version,
            name=inst_name,
            prov=definition.provider,
            trans=definition.transport,
            defn=definition_json,
            # bind 名は予約語を避ける(store.py の installer 教訓に倣う)。
            registrar=registered_by,
        )
        conn.commit()
        cur.execute(
            f"SELECT {_INSTANCE_COLS} FROM connector_instances WHERE id = :id",
            id=inst_id,
        )
        rec = _instance_row_to_record(cur.fetchone())

    rec["composition"] = report.model_dump()
    return rec


def get_connector(instance_id: str) -> dict[str, Any] | None:
    """登録済みコネクタを id で取得する。無ければ None。"""
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT {_INSTANCE_COLS} FROM connector_instances WHERE id = :id",
            id=instance_id,
        )
        row = cur.fetchone()
        return _instance_row_to_record(row) if row else None


def list_connectors(
    plugin_id: str | None = None, provider: str | None = None
) -> list[dict[str, Any]]:
    """登録済みコネクタを新しい順に一覧する。plugin_id / provider で絞り込める。"""
    where = []
    binds: dict[str, Any] = {}
    if plugin_id is not None:
        where.append("plugin_id = :pid")
        binds["pid"] = plugin_id
    if provider is not None:
        where.append("provider = :prov")
        binds["prov"] = provider
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT {_INSTANCE_COLS} FROM connector_instances{clause}
            ORDER BY created_at DESC FETCH FIRST 500 ROWS ONLY
            """,
            **binds,
        )
        return [_instance_row_to_record(r) for r in cur.fetchall()]


def remove_connector(instance_id: str) -> bool:
    """登録済みコネクタを削除する。削除した行があれば True。"""
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM connector_instances WHERE id = :id", id=instance_id
        )
        removed = cur.rowcount
        conn.commit()
    return removed > 0
