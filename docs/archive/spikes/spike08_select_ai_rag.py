"""SPIKE-08: Select AI RAG（DBMS_CLOUD_AIベクトル索引 + narrate）実機検証。

jetusedev ADB上で バケット文書 → ADB内ベクトル索引 → SELECT AI narrate のRAGを検証。
SPIKE-03と同一文書・同一質問セットを使い、RAG-04比較ドキュメントの定量データを取る。

実行: .venv/bin/python spikes/spike08_select_ai_rag.py [--qa-only]
前提: jetusedevウォレット /tmp/jetusedev_wallet、SPIKE-03文書を rag-sai/ プレフィックスにアップロード済み
"""

import json
import pathlib
import sys
import time

import oracledb

oracledb.defaults.fetch_lobs = False  # CLOBをstrで受ける

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV = dict(
    line.split("=", 1)
    for line in (ROOT / ".env").read_text().splitlines()
    if "=" in line and not line.startswith("#")
)
TFVARS = (ROOT / "infra/terraform/environments/dev/terraform.tfvars").read_text()
WALLET_PW = next(
    line.split('"')[1] for line in TFVARS.splitlines() if "ADB_WALLET_PASSWORD" in line
)
WALLET = "/tmp/jetusedev_wallet"

PROFILE = "JETUSE_RAG_AI"
INDEX = "JETUSE_RAG_IDX"
LLM = "meta.llama-3.3-70b-instruct"
EMBED = "cohere.embed-multilingual-v3.0"
LOCATION = (
    f"https://objectstorage.{ENV['OCI_REGION']}.oraclecloud.com"
    f"/n/{ENV['OS_NAMESPACE']}/b/jetuse-dev-app-data/o/rag-sai"
)

QUESTIONS = [
    ("出張の定義を教えてください", "50キロ"),
    ("新幹線のグリーン車を利用できるのは誰ですか", "部長"),
    ("管理職の国内出張の日当はいくらですか", "3,000"),
    ("一般職が東京23区内に宿泊する場合の宿泊費上限は", "12,000"),
    ("出張から戻った後、いつまでに精算が必要ですか", "5営業日"),
    ("在宅勤務は週に何日までできますか", "3日"),
    ("在宅勤務手当の金額と支給されない条件は", "3,000"),
    ("在宅勤務でカフェの公衆Wi-Fiを使ってもいいですか", "禁止"),
    ("領収書が必須になるのはいくら以上の経費ですか", "5,000"),
    ("タクシーを利用できるのはどんな場合ですか", "終電"),
]


def connect():
    import os

    dsn = os.environ.get("SPIKE_DSN", "jetusedev_low")
    wallet = os.environ.get("SPIKE_WALLET", WALLET)
    wallet_pw = os.environ.get("SPIKE_WALLET_PW", WALLET_PW)
    return oracledb.connect(
        user="ADMIN", password=ENV["ADB_ADMIN_PASSWORD"], dsn=dsn,
        config_dir=wallet, wallet_location=wallet, wallet_password=wallet_pw,
        tcp_connect_timeout=20.0,
    )


def setup(conn):
    cur = conn.cursor()
    print("== credential ==")
    oci_conf = dict(
        line.replace(" ", "").split("=", 1)
        for line in pathlib.Path("~/.oci/config").expanduser().read_text().splitlines()
        if "=" in line
    )
    key = pathlib.Path(oci_conf["key_file"]).expanduser().read_text()
    try:
        cur.execute("""
            BEGIN
              DBMS_CLOUD.CREATE_CREDENTIAL(
                credential_name => 'JETUSE_OCI_CRED',
                user_ocid       => :u,
                tenancy_ocid    => :t,
                private_key     => :k,
                fingerprint     => :f);
            END;""",
            u=oci_conf["user"], t=oci_conf["tenancy"],
            # OCI_API_KEYマーカー行の除去必須(SPIKE-04ハマり)
            k="".join(
                line for line in key.splitlines()
                if line and "-----" not in line and line != "OCI_API_KEY"
            ),
            f=oci_conf["fingerprint"])
        print("created")
    except oracledb.DatabaseError as e:
        if "ORA-20022" in str(e) or "already exists" in str(e):
            print("already exists")
        else:
            raise

    print("== profile ==")
    cur.execute(
        "BEGIN DBMS_CLOUD_AI.DROP_PROFILE(:p, force => TRUE); END;", p=PROFILE
    )
    attrs = {
        "provider": "oci",
        "credential_name": "JETUSE_OCI_CRED",
        "region": ENV["OCI_REGION"],
        "model": LLM,
        "embedding_model": EMBED,
        "vector_index_name": INDEX,
    }
    cur.execute(
        "BEGIN DBMS_CLOUD_AI.CREATE_PROFILE(:p, :a); END;",
        p=PROFILE, a=json.dumps(attrs),
    )
    print("created:", attrs)

    print("== vector index ==")
    try:
        cur.execute(
            "BEGIN DBMS_CLOUD_AI.DROP_VECTOR_INDEX(:i, include_data => TRUE); END;",
            i=INDEX,
        )
    except oracledb.DatabaseError:
        pass
    vattrs = {
        "vector_db_provider": "oracle",
        "location": LOCATION,
        "object_storage_credential_name": "JETUSE_OCI_CRED",
        "profile_name": PROFILE,
        "vector_distance_metric": "cosine",
        "chunk_size": 1024,
        "chunk_overlap": 128,
        "refresh_rate": 1440,
    }
    cur.execute(
        "BEGIN DBMS_CLOUD_AI.CREATE_VECTOR_INDEX(:i, :a); END;",
        i=INDEX, a=json.dumps(vattrs),
    )
    print("created:", vattrs["location"])
    conn.commit()


def wait_index(conn, timeout=900):
    """ベクトル表(INDEX$VECTAB)に行が入るまで待つ"""
    cur = conn.cursor()
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            cur.execute(f'SELECT COUNT(*) FROM "{INDEX}$VECTAB"')
            n = cur.fetchone()[0]
            print(f"  vectab rows: {n} ({int(time.time() - t0)}s)")
            if n > 0:
                return n
        except oracledb.DatabaseError as e:
            print(f"  vectab not ready: {str(e)[:80]} ({int(time.time() - t0)}s)")
        time.sleep(20)
    return 0


def qa(conn):
    cur = conn.cursor()
    ok = 0
    lat = []
    for q, kw in QUESTIONS:
        t0 = time.time()
        try:
            cur.execute(
                """SELECT DBMS_CLOUD_AI.GENERATE(
                     prompt => :q, profile_name => :p, action => 'narrate') FROM dual""",
                q=q, p=PROFILE,
            )
            ans = cur.fetchone()[0] or ""
            dt = time.time() - t0
            lat.append(dt)
            hit = kw in ans
            ok += hit
            print(f"[{'○' if hit else '×'}] ({dt:.1f}s) {q}\n    -> {ans[:140]}")
        except Exception as e:
            print(f"[NG] {q}: {str(e)[:200]}")
    if lat:
        lat.sort()
        print(f"\n正答(キーワード): {ok}/{len(QUESTIONS)}  "
              f"レイテンシ中央値: {lat[len(lat) // 2]:.1f}s  範囲: {lat[0]:.1f}-{lat[-1]:.1f}s")


if __name__ == "__main__":
    conn = connect()
    if "--qa-only" not in sys.argv:
        setup(conn)
        n = wait_index(conn)
        print(f"index ready: {n} chunks")
    qa(conn)
