"""方式②: Select AI のベクトル索引（DBMS_CLOUD_AI.CREATE_VECTOR_INDEX）の実機検証。

検証したいこと（tasks/SPIKE-M1.md 完了条件）:
  - `attributes` に**任意のキー**を載せられるか（載らないなら実際に何が入るか）
  - 載らない場合、`$VECTAB` への列追加や JOIN で補えるか（DB 内であることの利点が効くか）
  - Select AI の検索経路（GENERATE / RETRIEVAL）でメタデータ絞り込みができるか

実行: PYTHONPATH=spikes/spike_m1 .venv/bin/python spikes/spike_m1/method_b_select_ai.py
"""

import json
import sys
import time

import oracledb

from common import (
    CRED,
    RESOURCE_TAG,
    assert_compartment,
    banner,
    client_args,
    connect_spike,
    is_ours,
    load_env,
    record_created,
    require_owned_schema,
)
from fixtures import QUERY, chunks

BUCKET = "jetuse-spike-m1"          # jetuse-spike- 接頭辞必須（片付け対象）
PREFIX = "chunks/"
PROFILE = "JETUSE_SPIKE_M1_PROF"
INDEX = "JETUSE_SPIKE_M1_IDX"
VECTAB = f"{INDEX}$VECTAB"
LLM = "meta.llama-3.3-70b-instruct"
EMBED = "cohere.embed-multilingual-v3.0"


def _os_client():
    import oci

    return oci.object_storage.ObjectStorageClient(**client_args())


def upload_fixtures() -> str:
    """架空チャンクを 1 件 1 オブジェクトでバケットへ置く（Select AI 索引の入力）。"""
    banner("②-1 検証用バケットへ架空チャンクを配置")
    import oci

    load_env()
    compartment = assert_compartment()   # 未承認コンパートメントへ作らない（fail-closed）
    cli = _os_client()
    ns = cli.get_namespace().data
    try:
        created = cli.create_bucket(ns, oci.object_storage.models.CreateBucketDetails(
            name=BUCKET, compartment_id=compartment,
            metadata={"purpose": RESOURCE_TAG})).data
        # 名前は再利用されうるので、作成のたびに変わる bucket OCID を台帳 ID にする
        record_created("bucket", created.id, BUCKET)
        print(f"  created bucket {BUCKET}")
    except oci.exceptions.ServiceError as e:
        # 409(既存)だけを続行させる。認証エラー等を「既存」と誤分類しない
        if e.status != 409:
            raise
        # 既存バケットへ put_object して他人のオブジェクトを上書きしない。
        # purpose メタデータは誰でも書き換えられるので所有の根拠にしない。
        # **台帳の OCID だけ**を見る。
        if not is_ours("bucket", cli.get_bucket(ns, BUCKET).data.id):
            raise SystemExit(
                f"バケット {BUCKET} は既存だが台帳に無い（作り直された可能性を含む）。"
                " 他者のバケットの可能性があるため中止する（手動で確認すること）") from e
        print(f"  既存バケットを再利用（自分のものと確認済み）: {BUCKET}")
    for c in chunks():
        # ファイル名にメタデータを埋め込む＝Select AI で唯一取り得る「出典の粒度」を測るため
        name = (f"{PREFIX}{c['chunk_id']}__v{c['version']}__"
                f"{'current' if c['current_version'] else 'stale'}__{c['kind']}.txt")
        cli.put_object(ns, BUCKET, name, c["text"].encode("utf-8"))
    print(f"  uploaded {len(chunks())} objects under {BUCKET}/{PREFIX}")
    return (f"https://objectstorage.{load_env()['OCI_REGION']}.oraclecloud.com"
            f"/n/{ns}/b/{BUCKET}/o/{PREFIX.rstrip('/')}")


def create_index(cur: oracledb.Cursor, conn, location: str) -> None:
    banner("②-2 プロファイル + ベクトル索引の作成")
    for stmt, name in ((f"BEGIN DBMS_CLOUD_AI.DROP_VECTOR_INDEX('{INDEX}'); END;", INDEX),
                       (f"BEGIN DBMS_CLOUD_AI.DROP_PROFILE('{PROFILE}'); END;", PROFILE)):
        try:
            cur.execute(stmt)
            print(f"  dropped {name}")
        except oracledb.DatabaseError as e:
            print(f"  drop skip {name}: {str(e).splitlines()[0]}")
    prof_attrs = {
        "provider": "oci", "credential_name": CRED, "region": load_env()["OCI_REGION"],
        "model": LLM, "embedding_model": EMBED, "vector_index_name": INDEX,
    }
    print(f"  CREATE_PROFILE attributes={json.dumps(prof_attrs)}")
    cur.execute("BEGIN DBMS_CLOUD_AI.CREATE_PROFILE(:p, :a); END;",
                p=PROFILE, a=json.dumps(prof_attrs))
    idx_attrs = {
        "vector_db_provider": "oracle",
        "location": location,
        "object_storage_credential_name": CRED,
        "profile_name": PROFILE,
        "vector_distance_metric": "cosine",
        "chunk_size": 1024,
        "chunk_overlap": 64,
        "refresh_rate": 1440,
    }
    print(f"  CREATE_VECTOR_INDEX attributes={json.dumps(idx_attrs)}")
    cur.execute("BEGIN DBMS_CLOUD_AI.CREATE_VECTOR_INDEX(:i, :a); END;",
                i=INDEX, a=json.dumps(idx_attrs))
    conn.commit()
    deadline = time.time() + 300
    while time.time() < deadline:
        try:
            cur.execute(f'SELECT COUNT(*) FROM "{VECTAB}"')
            n = cur.fetchone()[0]
            # 1 行入っただけで完了とみなすと、一部しか取り込まれていない索引で
            # 版フィルタやレイテンシを測ってしまう
            if n >= len(chunks()):
                print(f"  {VECTAB} 行数={n}（索引構築完了）")
                return
        except oracledb.DatabaseError:
            pass
        time.sleep(5)
    raise RuntimeError(f"{VECTAB} に行が入らないままタイムアウト（以降の検証は成立しない）")


def try_arbitrary_attributes(cur: oracledb.Cursor) -> None:
    """CREATE_VECTOR_INDEX の attributes に任意キーを渡せるかを実際に叩いて確かめる。"""
    banner("②-3 CREATE_VECTOR_INDEX の attributes に任意キーを載せられるか")
    attrs = {
        "vector_db_provider": "oracle", "location": "https://example.invalid/o/x",
        "object_storage_credential_name": CRED, "profile_name": PROFILE,
        # ↓ 任意メタデータのつもりのキー
        "current_version": "Y", "kind": "spec", "sheet": "制約",
    }
    print(f"  CREATE_VECTOR_INDEX('{INDEX}_ARB', {json.dumps(attrs, ensure_ascii=False)})")
    try:
        cur.execute("BEGIN DBMS_CLOUD_AI.CREATE_VECTOR_INDEX(:i, :a); END;",
                    i=f"{INDEX}_ARB", a=json.dumps(attrs))
        print("  -> 受理された（後始末で drop する）")
        try:
            cur.execute(f"BEGIN DBMS_CLOUD_AI.DROP_VECTOR_INDEX('{INDEX}_ARB'); END;")
        except oracledb.DatabaseError:
            pass
    except oracledb.DatabaseError as e:
        print("  -> 拒否（エラー全文）:")
        print("     " + str(e).replace("\n", "\n     "))


def inspect_vectab(cur: oracledb.Cursor) -> None:
    banner("②-4 $VECTAB の実体（列構成と attributes の中身）")
    cur.execute(
        "SELECT column_name, data_type FROM user_tab_columns "
        "WHERE table_name = :t ORDER BY column_id", t=VECTAB)
    rows = cur.fetchall()
    print("  列構成:", ", ".join(f"{c}:{d}" for c, d in rows) or "（表が無い）")
    if not rows:
        return
    cur.execute(f'SELECT JSON_SERIALIZE(attributes PRETTY) FROM "{VECTAB}" FETCH FIRST 2 ROWS ONLY')
    for i, (a,) in enumerate(cur.fetchall(), 1):
        print(f"  --- attributes サンプル {i} ---")
        print("  " + (a or "").replace("\n", "\n  "))


def try_extend_vectab(cur: oracledb.Cursor, conn) -> bool:
    """$VECTAB を DB 側で拡張できるか（列追加 / JOIN）を実機で確かめる。"""
    banner("②-5 $VECTAB を列追加・JOIN で補えるか（DB 内であることの利点）")
    ok_alter = False
    try:
        cur.execute(f'ALTER TABLE "{VECTAB}" ADD (current_version CHAR(1))')
        print("  ALTER TABLE ... ADD (current_version CHAR(1)) -> OK")
        ok_alter = True
    except oracledb.DatabaseError as e:
        print("  ALTER TABLE -> NG（エラー全文）:")
        print("     " + str(e).replace("\n", "\n     "))
    if ok_alter:
        cur.execute(
            f'''UPDATE "{VECTAB}" SET current_version =
                CASE WHEN JSON_VALUE(attributes, '$.object_name') LIKE '%__stale__%'
                     THEN 'N' ELSE 'Y' END''')
        conn.commit()
        cur.execute(f'SELECT current_version, COUNT(*) FROM "{VECTAB}" GROUP BY current_version')
        print("  補完後の内訳:", sorted(cur.fetchall()))
    return ok_alter


def search_vectab(cur: oracledb.Cursor, extended: bool) -> None:
    """$VECTAB に対して直接ベクタ検索をかけ、メタデータ絞り込みが効くか見る。"""
    banner("②-6 $VECTAB への直接ベクタ検索（フィルタ無し / 版フィルタ）")
    cur.execute(
        "SELECT column_name FROM user_tab_columns WHERE table_name = :t AND data_type = 'VECTOR'",
        t=VECTAB)
    vcol = cur.fetchone()
    if not vcol:
        print("  VECTOR 列が見つからない。スキップ")
        return
    vcol = vcol[0]
    import array

    from jetuse_core.embeddings import embed

    qv = array.array("f", embed([QUERY], input_type="SEARCH_QUERY")[0])
    for label, where in [("フィルタ無し", ""),
                         ("版フィルタ", "WHERE current_version = 'Y'" if extended else None)]:
        if where is None:
            print(f"\n  [{label}] $VECTAB を拡張できなかったため実施不能")
            continue
        sql = (f'''SELECT JSON_VALUE(attributes, '$.object_name') AS object_name,
                      ROUND(VECTOR_DISTANCE({vcol}, :q, COSINE), 4) AS dist
               FROM "{VECTAB}" {where}
               ORDER BY VECTOR_DISTANCE({vcol}, :q, COSINE) FETCH FIRST 5 ROWS ONLY''')
        print(f"\n  [{label}]\n  --- SQL ---\n  {sql}")
        cur.execute(sql, q=qv)
        for obj, dist in cur.fetchall():
            print(f"    {obj} | {dist}")


def try_select_ai_generate(cur: oracledb.Cursor) -> bool:
    banner("②-7 Select AI GENERATE(narrate) が返す出典の粒度")
    try:
        cur.execute(
            "SELECT DBMS_CLOUD_AI.GENERATE(prompt => :q, profile_name => :p, "
            "action => 'narrate') FROM dual", q=QUERY, p=PROFILE)
        ans = cur.fetchone()[0] or ""
        print("  --- 応答全文 ---")
        print("  " + ans.replace("\n", "\n  "))
        return True
    except oracledb.DatabaseError as e:
        print("  NG（エラー全文）:")
        print("     " + str(e).replace("\n", "\n     "))
        return False


def main() -> None:
    conn = connect_spike()
    require_owned_schema(conn)   # 単独実行でも「自分のスキーマか」を必ず確認
    cur = conn.cursor()
    location = upload_fixtures()
    create_index(cur, conn, location)
    try_arbitrary_attributes(cur)
    inspect_vectab(cur)
    extended = try_extend_vectab(cur, conn)
    search_vectab(cur, extended)
    ok = try_select_ai_generate(cur)
    conn.close()
    if not ok:
        sys.exit("Select AI GENERATE が失敗した。② の検証は成立していない")


if __name__ == "__main__":
    main()
