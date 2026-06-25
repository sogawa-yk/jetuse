"""インストール済みプラグインの記録リポジトリ(PLG-02)。

スナップショット取込(ADR-0013 D6 / 版固定)の永続化層。取り込んだプラグインの
出所(`source_registry`)・版・署名検証状態・manifest 全文を `installed_plugins` に記録し、
出所追跡を可能にする。レジストリ通信・署名検証そのものは PLG-03 の責務で、ここは
記録の CRUD のみを提供する(`signature_verified` は呼び出し側が検証結果を渡す)。

manifest は配布表現(camelCase, `model_dump(by_alias=True)`)のまま CLOB に保存し、
取り出し時に dict へ戻す。`installed_plugins` の `(plugin_id, version)` は一意であり、
同一版の二重記録は DB が拒否する(版固定スナップショットは版あたり 1 件)。
"""

import json
import uuid
from typing import Any

from ..db import connect
from .manifest import PluginManifest

# 取得系で共通に並べる列。順序は _row_to_record と一致させる。
_COLS = (
    "id, plugin_id, version, kind, source_registry, manifest, "
    "signature_verified, installed_by, installed_at"
)

# DB カラム幅(VARCHAR2)と一致させる入力上限。超えると保存時に ORA-12899 になるため、
# manifest の id/version と同様に書き込み境界で弾いて予測可能な ValueError にする。
MAX_INSTALLED_BY_LEN = 255
MAX_SOURCE_REGISTRY_LEN = 255


def _uid() -> str:
    return str(uuid.uuid4())


def _row_to_record(r) -> dict[str, Any]:
    installed_at = r[8]
    # manifest CLOB は record_install で検証済み JSON のみ書き込むが、読み取り境界は
    # fail-soft にする。万一壊れた/非 JSON の CLOB があっても list_installs 全体を例外で
    # 巻き添えにせず、当該レコードを manifest=None + manifest_error で識別可能にして返す。
    manifest: dict | None
    manifest_error = False
    raw = r[5]
    # CLOB は db 既定(fetch_lobs=False)で str で返るが、設定差で LOB オブジェクトが
    # 返る構成にも備えて read() があれば文字列化してから decode する。
    if raw is not None and hasattr(raw, "read"):
        raw = raw.read()
    if raw:
        try:
            manifest = json.loads(raw)
        except (ValueError, TypeError):
            manifest = None
            manifest_error = True
    else:
        manifest = None
    return {
        "id": r[0],
        "plugin_id": r[1],
        "version": r[2],
        "kind": r[3],
        "source_registry": r[4],
        "manifest": manifest,
        "manifest_error": manifest_error,
        "signature_verified": bool(r[6]),
        "installed_by": r[7],
        # TIMESTAMP は datetime で返る。fake/文字列保存にも備えて isoformat があれば使う。
        "installed_at": installed_at.isoformat()
        if hasattr(installed_at, "isoformat")
        else installed_at,
    }


def record_install(
    installed_by: str,
    manifest: PluginManifest,
    *,
    source_registry: str | None = None,
    signature_verified: bool = False,
) -> dict[str, Any]:
    """検証済み manifest を 1 件のインストール記録として永続化し、保存後の記録を返す。

    `plugin_id`/`version`/`kind` は manifest から刻む(出所追跡の正本)。`installed_at` は
    DB 既定値で確定するため、INSERT 後に同一接続で読み戻して返す。

    トランザクション境界: 本関数は `installed_plugins` 1 表への単一原子操作(他リポジトリ
    [usecases.py/agents.py]と同じく内部で commit する規約)。スナップショット取込で
    「記録 + usecases/agents への取込・source_* 刻印」を 1 トランザクションにまとめる必要は
    PLG-03(取込)/PLG-07(ローダー)のサービス層の責務で、そこで束ねる。本タスク(PLG-02)は
    記録 CRUD のみを提供する(tasks/PLG-02.md 非ゴール)。

    `installed_by`(非空・255 以内)と `source_registry`(None または 255 以内)は DB カラム幅で
    弾く前に検証し、長すぎ・空の入力は環境依存の DB エラーでなく ValueError として返す。
    """
    if not installed_by or not installed_by.strip():
        raise ValueError("installed_by は非空でなければならない")
    if len(installed_by) > MAX_INSTALLED_BY_LEN:
        raise ValueError(
            f"installed_by は {MAX_INSTALLED_BY_LEN} 文字以内でなければならない"
        )
    if source_registry is not None and len(source_registry) > MAX_SOURCE_REGISTRY_LEN:
        raise ValueError(
            f"source_registry は {MAX_SOURCE_REGISTRY_LEN} 文字以内でなければならない"
        )
    rec_id = _uid()
    payload = json.dumps(manifest.model_dump(by_alias=True), ensure_ascii=False)
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO installed_plugins(
              id, plugin_id, version, kind, source_registry, manifest,
              signature_verified, installed_by)
            VALUES (:id, :pid, :ver, :kind, :reg, :man, :sig, :installer)
            """,
            id=rec_id,
            pid=manifest.id,
            ver=manifest.version,
            kind=manifest.kind,
            reg=source_registry,
            man=payload,
            sig=1 if signature_verified else 0,
            # bind 名は予約語(BY)不可=ORA-01745。`installer` を使う(実機 E2E で検出)。
            installer=installed_by,
        )
        conn.commit()
        cur.execute(
            f"SELECT {_COLS} FROM installed_plugins WHERE id = :id", id=rec_id
        )
        return _row_to_record(cur.fetchone())


def get_install(install_id: str) -> dict[str, Any] | None:
    """インストール記録を id で取得する。無ければ None。"""
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT {_COLS} FROM installed_plugins WHERE id = :id", id=install_id
        )
        row = cur.fetchone()
        return _row_to_record(row) if row else None


def find_install(plugin_id: str, version: str) -> dict[str, Any] | None:
    """plugin_id + version でインストール記録を取得する(版固定の照合)。無ければ None。"""
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT {_COLS} FROM installed_plugins
            WHERE plugin_id = :pid AND version = :ver
            """,
            pid=plugin_id,
            ver=version,
        )
        row = cur.fetchone()
        return _row_to_record(row) if row else None


def list_installs(plugin_id: str | None = None) -> list[dict[str, Any]]:
    """インストール記録を新しい順に一覧する。plugin_id 指定でその版だけに絞る。"""
    with connect() as conn:
        cur = conn.cursor()
        if plugin_id is None:
            cur.execute(
                f"""
                SELECT {_COLS} FROM installed_plugins
                ORDER BY installed_at DESC FETCH FIRST 500 ROWS ONLY
                """
            )
        else:
            cur.execute(
                f"""
                SELECT {_COLS} FROM installed_plugins
                WHERE plugin_id = :pid
                ORDER BY installed_at DESC FETCH FIRST 500 ROWS ONLY
                """,
                pid=plugin_id,
            )
        return [_row_to_record(r) for r in cur.fetchall()]


def set_signature_verified(install_id: str, verified: bool) -> bool:
    """記録の署名検証状態を更新する。対象が無ければ False。"""
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE installed_plugins SET signature_verified = :sig WHERE id = :id
            """,
            sig=1 if verified else 0,
            id=install_id,
        )
        conn.commit()
        return cur.rowcount > 0


def delete_install(install_id: str) -> bool:
    """インストール記録を削除する(アンインストール)。対象が無ければ False。"""
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM installed_plugins WHERE id = :id", id=install_id
        )
        conn.commit()
        return cur.rowcount > 0
