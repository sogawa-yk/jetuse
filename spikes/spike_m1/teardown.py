"""SPIKE-M1 が作った検証用リソースを片付ける（tasks/SPIKE-M1.md「作ったら片付ける」）。

**名前一致では消さない。** OCI 側リソース（Vector Store / バケット / GenAI プロジェクト）は
台帳 `.spike-m1-registry.json`（gitignore 済み・リポジトリ直下）に
自分で作ったと記録された ID だけを消す。
証跡側にあるのは名前だけの写し `runs/<run-id>/e2e/created-resources-names.json`。
台帳に無いものは同名でも触らない（他者のリソースを壊さないため）。

ADB 側は接続先ガード（common.assert_target）を通したうえで、スパイク専用スキーマ
JETUSE_SPIKE_M1 の中のオブジェクトとスキーマ自体を消す。

既定は dry-run。実際に消すには --yes を付ける（CLAUDE.md「破壊的スクリプトは明示フラグ必須」）。
実行: PYTHONPATH=spikes/spike_m1 .venv/bin/python spikes/spike_m1/teardown.py --yes
"""

import sys

import oci
import oracledb

from common import (
    CRED,
    SCHEMA,
    VEC_CRED,
    banner,
    client_args,
    connect_admin,
    connect_spike,
    forget_created,
    is_ours,
    load_env,
    registry,
    schema_key,
)
from method_a_vector_store import PROJECT_NAME, VS_NAME, _clients
from method_b_select_ai import BUCKET, INDEX, PROFILE, _os_client
from method_c_own_index import TABLE

APPLY = "--yes" in sys.argv
# エンドポイントはコードに実値を埋めずリージョンから組み立てる（setup_schema と同じ形）
ACL_HOST_TEMPLATES = (
    "inference.generativeai.{region}.oci.oraclecloud.com",
    "generativeai.{region}.oci.oraclecloud.com",
    "objectstorage.{region}.oraclecloud.com",
)

# 片付け中の失敗は握り潰さず集める。1 つ失敗しても残りの片付けは続け、最後に非ゼロ終了する。
FAILURES: list[str] = []


def _step(label: str, fn) -> None:
    if not APPLY:
        print(f"  [dry-run] {label}")
        return
    try:
        fn()
        print(f"  ok: {label}")
    except Exception as e:  # noqa: BLE001 - 片付けは最後まで走らせ、失敗は集約して報告する
        msg = str(e).splitlines()[0] if str(e) else type(e).__name__
        print(f"  NG: {label} ({msg})")
        FAILURES.append(f"{label}: {msg}")


def _sql(cur: oracledb.Cursor, label: str, sql: str, *, ok_codes: tuple[str, ...] = ()) -> None:
    def run():
        try:
            cur.execute(sql)
        except oracledb.DatabaseError as e:
            if any(code in str(e) for code in ok_codes):
                print(f"    （既に無い: {str(e).splitlines()[0]}）")
                return
            raise
    _step(label, run)


def drop_db_objects() -> None:
    banner(f"ADB: {SCHEMA} 内のオブジェクト")
    # OCI 側と同じく DB 側も台帳で門番する。台帳に無いスキーマは同名でも触らない
    # （この門番が無かったせいで、ガード確認の実行が本物のスキーマを消した）。
    try:
        conn = connect_spike()
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 - スキーマが既に無い場合はスキップして先へ
        print(f"  {SCHEMA} へ接続できない（既に削除済み?）: {type(e).__name__}")
        return
    if not is_ours("db_schema", schema_key(conn)):
        print(f"  台帳に {SCHEMA} が無い。名前一致では消さない（スキップ）")
        conn.close()
        return
    cur = conn.cursor()
    _sql(cur, f"DROP_VECTOR_INDEX {INDEX}",
         f"BEGIN DBMS_CLOUD_AI.DROP_VECTOR_INDEX('{INDEX}'); END;", ok_codes=("ORA-20048",))
    _sql(cur, f"DROP_PROFILE {PROFILE}",
         f"BEGIN DBMS_CLOUD_AI.DROP_PROFILE('{PROFILE}'); END;", ok_codes=("ORA-20046",))
    _sql(cur, f"DROP TABLE {TABLE}", f"DROP TABLE {TABLE} PURGE", ok_codes=("ORA-00942",))
    _sql(cur, f"DROP_CREDENTIAL {CRED}",
         f"BEGIN DBMS_CLOUD.DROP_CREDENTIAL('{CRED}'); END;", ok_codes=("ORA-20000",))
    _sql(cur, f"DROP_CREDENTIAL {VEC_CRED}",
         f"BEGIN DBMS_VECTOR_CHAIN.DROP_CREDENTIAL('{VEC_CRED}'); END;", ok_codes=("ORA-20000",))
    conn.close()


def drop_bucket() -> None:
    banner(f"Object Storage: {BUCKET}")
    recorded = registry().get("bucket", [])
    if not recorded:
        print("  台帳にバケットが無い。名前一致では消さない（スキップ）")
        return
    cli = _os_client()
    ns = cli.get_namespace().data
    try:
        bucket_id = cli.get_bucket(ns, BUCKET).data.id
    except oci.exceptions.ServiceError as e:
        if e.status != 404:
            raise
        print("  bucket は既に無い（404）")
        for r in recorded:
            forget_created("bucket", r["id"])
        return
    if not any(r["id"] == bucket_id for r in recorded):
        print(f"  {BUCKET} は台帳の OCID と一致しない（作り直された?）。触らずスキップ")
        return
    try:
        names = []
        start = None
        while True:   # ページングを打ち切らない（残ったオブジェクトで delete_bucket が失敗する）
            page = cli.list_objects(ns, BUCKET, start=start).data
            names.extend(o.name for o in page.objects)
            start = page.next_start_with
            if not start:
                break
    except oci.exceptions.ServiceError as e:
        # 404（既に無い）だけを正常扱い。権限・通信エラーを「片付け済み」と誤報告しない
        if e.status != 404:
            raise
        print("  bucket は既に無い（404）")
        return
    print(f"  objects: {len(names)}")
    for n in names:
        _step(f"delete_object {n}", lambda n=n: cli.delete_object(ns, BUCKET, n))
    _step(f"delete_bucket {BUCKET}", lambda: cli.delete_bucket(ns, BUCKET))
    if APPLY:
        try:
            cli.get_bucket(ns, BUCKET)
            FAILURES.append(f"bucket {BUCKET} が削除後も残っている")
        except oci.exceptions.ServiceError as e:
            if e.status != 404:
                raise
            print(f"  確認: bucket {BUCKET} は消えている（404）")
            forget_created("bucket", bucket_id)


def drop_vector_store() -> None:
    banner(f"OCI Vector Store: {VS_NAME}")
    recorded = {r["id"] for r in registry().get("vector_store", [])}
    if not recorded:
        print("  台帳に Vector Store が無い。名前一致では消さない（スキップ）")
        return
    # 片付けでリソースを新規作成しない（dry-run でも作らせない）
    cp, dp = _clients(allow_create=False)
    seen = set()
    stores = []
    after = None
    while True:   # 一覧はページングする（記録 ID が 1 ページ目に無いと取りこぼす）
        page = cp.vector_stores.list(after=after) if after else cp.vector_stores.list()
        stores.extend(page.data)
        if not getattr(page, "has_more", False) or not page.data:
            break
        after = page.data[-1].id
    for vs in stores:
        if vs.id not in recorded or vs.id in seen:
            continue
        seen.add(vs.id)
        vs_id = vs.id
        files = []
        after = None
        while True:   # 1 ページ目だけ消して「片付いた」と言わない
            page = (dp.vector_stores.files.list(vector_store_id=vs_id, after=after)
                    if after else dp.vector_stores.files.list(vector_store_id=vs_id))
            files.extend(f.id for f in page.data)
            if not getattr(page, "has_more", False) or not page.data:
                break
            after = page.data[-1].id
        print(f"  vector store {vs_id} / files {len(files)}")
        for fid in files:
            # ストアからの切り離しと Files 本体の削除は別々に扱う
            # （まとめると本体削除だけ失敗したときに孤立ファイルを見失う）
            _step(f"detach file {fid}",
                  lambda fid=fid, v=vs_id: dp.vector_stores.files.delete(
                      vector_store_id=v, file_id=fid))
            _step(f"delete file {fid}", lambda fid=fid: dp.files.delete(fid))
        _step(f"delete vector store {vs_id}",
              lambda v=vs_id: cp.vector_stores.delete(vector_store_id=v))
        if APPLY:
            still = [v.id for v in cp.vector_stores.list().data if v.id == vs_id]
            if still:
                FAILURES.append(f"vector store {vs_id} が削除後も残っている")
            else:
                print(f"  確認: vector store {vs_id} は消えている")
                forget_created("vector_store", vs_id)
    missing = recorded - seen
    if missing:
        FAILURES.append(f"台帳の vector store が一覧に見つからない: {sorted(missing)}")


def drop_project() -> None:
    banner(f"GenAI プロジェクト: {PROJECT_NAME}")
    recorded = {r["id"] for r in registry().get("genai_project", [])}
    if not recorded:
        print("  台帳に GenAI プロジェクトが無い。名前一致では消さない（スキップ）")
        return
    import oci
    from jetuse_core.settings import get_settings

    s = get_settings()
    gai = oci.generative_ai.GenerativeAiClient(**client_args())
    found: set[str] = set()
    for p in oci.pagination.list_call_get_all_results(
            gai.list_generative_ai_projects, s.compartment_ocid).data:
        if p.id in recorded and p.lifecycle_state != "DELETED":
            _step(f"delete GenAI project {p.display_name}",
                  lambda p=p: gai.delete_generative_ai_project(p.id))
            found.add(p.id)
            if APPLY:
                state = gai.get_generative_ai_project(p.id).data.lifecycle_state
                print(f"  確認: GenAI project {p.display_name} state={state}")
                if state in ("DELETED", "DELETING"):
                    forget_created("genai_project", p.id)
                else:
                    FAILURES.append(f"GenAI project {p.display_name} が削除されていない（{state}）")
    missing = recorded - found
    if missing:
        FAILURES.append(f"台帳の GenAI project が一覧に見つからない: {sorted(missing)}")


def drop_schema() -> None:
    banner(f"ADB: スキーマ {SCHEMA} 自体")
    admin = connect_admin()
    if not is_ours("db_schema", schema_key(admin)):
        print(f"  台帳に {SCHEMA} が無い。名前一致では消さない（スキップ）")
        admin.close()
        return
    cur = admin.cursor()
    region = load_env()["OCI_REGION"]
    for host in (h.format(region=region) for h in ACL_HOST_TEMPLATES):
        _sql(cur, f"REMOVE_HOST_ACE {host}",
             "BEGIN DBMS_NETWORK_ACL_ADMIN.REMOVE_HOST_ACE("
             f"host => '{host}', "
             "ace => xs$ace_type(privilege_list => xs$name_list('http'), "
             f"principal_name => '{SCHEMA}', principal_type => xs_acl.ptype_db), "
             "remove_empty_acl => TRUE); END;", ok_codes=("ORA-46057", "ORA-24244"))
    key = schema_key(admin)
    _sql(cur, f"DROP USER {SCHEMA} CASCADE", f"DROP USER {SCHEMA} CASCADE",
         ok_codes=("ORA-01918",))
    if APPLY:
        cur.execute("SELECT COUNT(*) FROM all_users WHERE username = :u", u=SCHEMA)
        if cur.fetchone()[0]:
            FAILURES.append(f"{SCHEMA} が削除後も残っている")
        else:
            print(f"  確認: {SCHEMA} は消えている")
            forget_created("db_schema", key)
    admin.close()


def main() -> None:
    if not APPLY:
        print("※ dry-run（--yes を付けると実際に削除する）\n")
    # DB 側が失敗しても OCI 側の片付けまで必ず到達させる
    for fn in (drop_db_objects, drop_bucket, drop_vector_store, drop_project, drop_schema):
        try:
            fn()
        except Exception as e:  # noqa: BLE001 - 片付けは最後まで走らせる
            print(f"  NG: {fn.__name__} ({type(e).__name__}: {str(e)[:120]})")
            FAILURES.append(f"{fn.__name__}: {type(e).__name__}")
    if FAILURES:
        print("\n片付けに失敗した項目:")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("\ndone" if APPLY else "\ndone (dry-run)")


if __name__ == "__main__":
    main()
