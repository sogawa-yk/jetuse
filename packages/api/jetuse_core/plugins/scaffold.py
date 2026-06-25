"""sample-app の scaffold 取込ロジック(SBA-01)。

`kind: sample-app` の manifest を受け取り、合成バリデーション(必要ケイパビリティ/権限スコープ)
を通したうえで、定義(screens/datasets/aiSlots)と dataset の seed 行を
**インスタンス側 ADB へ展開**する。これにより取込後は `sample_app_instances`(定義)と
`sample_app_seed_rows`(シード)が ADB に出現する。

責務境界:
  - 定義の構造検証・合成バリデーションは `sample_app.py`。
    本モジュールは「ok を確認してから展開」する。
  - 合成バリデーションが致命的不足(必要ケイパビリティ不足・権限スコープ宣言違反)を検出した場合は
    **DB に何も書かず** `CompositionError` を送出する(fail-closed)。
  - レジストリ取込(installed_plugins への記録)は PLG-02/03 の責務。本モジュールは sample-app の
    展開のみを行う(installed_plugins とは plugin_id/source_version で対応づく)。

store.py と同じ規約: connect() で接続し、関数内で commit する単一表/関連表の原子操作。
manifest は配布表現(camelCase, model_dump(by_alias=True))のまま CLOB に格納する。
"""

import json
import uuid
from typing import Any

from ..db import connect
from .manifest import PluginManifest
from .sample_app import (
    CompositionError,
    SampleAppDefinition,
    validate_composition,
    validate_sample_app,
)

# DB カラム幅(VARCHAR2)と一致させる入力上限。超えると保存時に ORA-12899 になるため、
# 書き込み境界で弾いて予測可能な ValueError にする(store.py と同じ方針)。
MAX_CREATED_BY_LEN = 255
MAX_NAME_LEN = 200

_INSTANCE_COLS = (
    "id, plugin_id, source_version, name, definition, created_by, created_at"
)


def _uid() -> str:
    return str(uuid.uuid4())


def _read_clob(raw: Any) -> Any:
    # CLOB は db 既定(fetch_lobs=False)で str。設定差で LOB が返る構成にも備える。
    if raw is not None and hasattr(raw, "read"):
        raw = raw.read()
    return raw


def _instance_row_to_record(r) -> dict[str, Any]:
    created_at = r[6]
    definition: dict | None
    definition_error = False
    raw = _read_clob(r[4])
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
        "definition": definition,
        "definition_error": definition_error,
        "created_by": r[5],
        "created_at": created_at.isoformat()
        if hasattr(created_at, "isoformat")
        else created_at,
    }


def _seed_row_to_record(r) -> dict[str, Any]:
    payload: dict | None
    payload_error = False
    raw = _read_clob(r[3])
    if raw:
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            payload = None
            payload_error = True
    else:
        payload = None
    return {
        "id": r[0],
        "dataset": r[1],
        "row_index": r[2],
        "payload": payload,
        "payload_error": payload_error,
    }


def scaffold_sample_app(
    manifest: PluginManifest,
    *,
    created_by: str,
    available_capabilities: frozenset[str] | set[str] | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    """sample-app manifest をインスタンスへ展開し、保存後のインスタンス記録を返す。

    手順:
      1. 定義(contributes["sample-app"])を構造検証する。
      2. 合成バリデーション(必要ケイパビリティ/権限スコープ)を実行する。
         **致命的不足があれば DB に何も書かず CompositionError を送出**(fail-closed)。
      3. インスタンス行(定義 CLOB)＋ dataset ごとの seed 行を 1 トランザクションで挿入する。

    `available_capabilities` はホストインスタンスが備える JetUse 能力集合(None なら全コア能力)。
    不足する能力があれば展開を拒否する(E2E シナリオ: 必要ケイパビリティ不足の検出)。
    """
    if manifest.kind != "sample-app":
        raise ValueError(
            f"scaffold できるのは kind=sample-app の manifest のみ: {manifest.kind}"
        )
    if not created_by or not created_by.strip():
        raise ValueError("created_by は非空でなければならない")
    if len(created_by) > MAX_CREATED_BY_LEN:
        raise ValueError(f"created_by は {MAX_CREATED_BY_LEN} 文字以内でなければならない")

    definition: SampleAppDefinition = validate_sample_app(manifest)
    report = validate_composition(
        manifest,
        available_capabilities=available_capabilities,
        definition=definition,
    )
    if not report.ok:
        # fail-closed: 不足を検出したら一切展開しない。
        raise CompositionError(report)

    # name 未指定(None)のときだけ manifest.name にフォールバックする。明示的に空/空白を渡したら
    # サイレントにフォールバックせずエラーにする(呼び出し側の取り違えを検出)。
    inst_name = (manifest.name if name is None else name).strip()
    if not inst_name:
        raise ValueError("name は非空でなければならない")
    if len(inst_name) > MAX_NAME_LEN:
        raise ValueError(f"name は {MAX_NAME_LEN} 文字以内でなければならない")

    inst_id = _uid()
    # 定義は配布表現(camelCase: aiSlots)で保存し、取り出し時に往復できるようにする。
    definition_json = json.dumps(
        definition.model_dump(by_alias=True), ensure_ascii=False
    )

    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            INSERT INTO sample_app_instances(
              {_INSTANCE_COLS.replace(', created_at', '')})
            VALUES (:id, :pid, :ver, :name, :defn, :creator)
            """,
            id=inst_id,
            pid=manifest.id,
            ver=manifest.version,
            name=inst_name,
            defn=definition_json,
            # bind 名は予約語を避ける(store.py の installer 教訓に倣う)。
            creator=created_by,
        )
        # dataset ごとに seed 行を展開する。
        seed_rows = [
            (
                _uid(),
                inst_id,
                ds.name,
                idx,
                json.dumps(row, ensure_ascii=False),
            )
            for ds in definition.datasets
            for idx, row in enumerate(ds.seed)
        ]
        if seed_rows:
            cur.executemany(
                """
                INSERT INTO sample_app_seed_rows(
                  id, instance_id, dataset, row_index, payload)
                VALUES (:1, :2, :3, :4, :5)
                """,
                seed_rows,
            )
        conn.commit()
        cur.execute(
            f"SELECT {_INSTANCE_COLS} FROM sample_app_instances WHERE id = :id",
            id=inst_id,
        )
        rec = _instance_row_to_record(cur.fetchone())

    rec["seed_count"] = len(seed_rows)
    rec["composition"] = report.model_dump()
    return rec


def get_instance(instance_id: str) -> dict[str, Any] | None:
    """scaffold 済みインスタンスを id で取得する。無ければ None。"""
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT {_INSTANCE_COLS} FROM sample_app_instances WHERE id = :id",
            id=instance_id,
        )
        row = cur.fetchone()
        return _instance_row_to_record(row) if row else None


def list_instances(plugin_id: str | None = None) -> list[dict[str, Any]]:
    """scaffold 済みインスタンスを新しい順に一覧する。plugin_id で絞り込める。"""
    with connect() as conn:
        cur = conn.cursor()
        if plugin_id is None:
            cur.execute(
                f"""
                SELECT {_INSTANCE_COLS} FROM sample_app_instances
                ORDER BY created_at DESC FETCH FIRST 500 ROWS ONLY
                """
            )
        else:
            cur.execute(
                f"""
                SELECT {_INSTANCE_COLS} FROM sample_app_instances
                WHERE plugin_id = :pid
                ORDER BY created_at DESC FETCH FIRST 500 ROWS ONLY
                """,
                pid=plugin_id,
            )
        return [_instance_row_to_record(r) for r in cur.fetchall()]


def list_seed_rows(
    instance_id: str, dataset: str | None = None
) -> list[dict[str, Any]]:
    """インスタンスに展開された seed 行を dataset・row_index 順に返す。"""
    with connect() as conn:
        cur = conn.cursor()
        if dataset is None:
            cur.execute(
                """
                SELECT id, dataset, row_index, payload FROM sample_app_seed_rows
                WHERE instance_id = :iid
                ORDER BY dataset, row_index
                """,
                iid=instance_id,
            )
        else:
            cur.execute(
                """
                SELECT id, dataset, row_index, payload FROM sample_app_seed_rows
                WHERE instance_id = :iid AND dataset = :ds
                ORDER BY row_index
                """,
                iid=instance_id,
                ds=dataset,
            )
        return [_seed_row_to_record(r) for r in cur.fetchall()]


def delete_instance(instance_id: str) -> bool:
    """インスタンスと展開済み seed 行を削除する。対象が無ければ False。

    FK の ON DELETE CASCADE で seed 行は自動削除されるが、CASCADE 未対応の構成(fake/旧DB)でも
    確実に消えるよう seed 行を先に明示削除する(冪等)。
    """
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM sample_app_seed_rows WHERE instance_id = :iid",
            iid=instance_id,
        )
        cur.execute(
            "DELETE FROM sample_app_instances WHERE id = :id", id=instance_id
        )
        deleted = cur.rowcount > 0
        conn.commit()
        return deleted
