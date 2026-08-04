"""MEM-01: Oracle AI Agent Memory SDK (`oracleagentmemory` 26.6.0) を実 ADB で確かめる。

確かめること:
  ① SDK は接続先スキーマに何を作るのか（表・索引・ジョブ）
  ② LLM / 埋め込みを **JetUse 既存の IAM 署名経路**（`jetuse_core.oci_auth` 経由）で動かせるか。
     SDK 同梱の `Llm` / `Embedder` は litellm の `oci/` プロバイダで API キーか signer を要求する。
     ADR-0021 で API キーを持ち込まない方針なので、`ILlm` / `IEmbedder` を自前実装して差し替える。
  ③ 会話をまたいだ想起 / subject 分離 / スレッド削除での派生記憶の消滅

**期待値は assert する**（観測値を記録するだけでは検証にならない）。1 つでも外れたら非ゼロ終了する。

接続先は `spikes/ragm02/common.py` の解決を再利用する（ウォレット実体から DSN を引き、
`ops/_adb.assert_target()` で「承認済みコンパートメントの loop ADB か」を確かめる）。
**接続先の実値はここに書かない**（環境依存値は `.env` と生成ウォレットから解決する）。

実行:
  SPIKE_SCHEMA_PREFIX=JETUSE_MEM01 SPIKE_HOME=/tmp/jetuse-mem01 \
    PYTHONPATH=packages/api /tmp/mem01-venv/bin/python spikes/mem01/probe_sdk.py
"""

import json
import os
import pathlib
import sys

import numpy as np
import oracledb

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "spikes" / "ragm02"))

from jetuse_core import embeddings  # noqa: E402
from jetuse_core.genai import make_inference_client  # noqa: E402
from jetuse_core.settings import get_settings  # noqa: E402

import common  # noqa: E402  接続解決・所有台帳・fail-closed ゲートを再利用する

# このスパイクが触ってよいスキーマの接頭辞。**これ以外には DDL も DML も打たない**
# （誤った SPIKE_HOME / .env で他タスクのスキーマを書き換えないための fail-closed ゲート）。
ALLOWED_SCHEMA_PREFIX = "JETUSE_MEM01_"
STORE_ID = "MEM01"
# 抽出された記憶の種別（`search(record_types=)` に渡す値）。発話は `message`。
MEMORY_RECORD_TYPES = ["memory", "preference", "fact", "guideline"]
# 生成モデルは Chat Completions 系（`gpt-oss-120b` は Responses 専用で 404 になる）。
LLM_MODEL = "meta.llama-3.3-70b-instruct"


class OciEmbedder:
    """`IEmbedder` を既存の `jetuse_core.embeddings`（IAM 署名）で実装する。"""

    embedding_dimension = embeddings.EMBED_DIM
    max_input_tokens = 512

    def embed(self, texts: list[str], *, is_query: bool = False) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.embedding_dimension), dtype=np.float32)
        vectors = embeddings.embed(
            texts, input_type="SEARCH_QUERY" if is_query else "SEARCH_DOCUMENT"
        )
        return np.asarray(vectors, dtype=np.float32)

    async def embed_async(self, texts: list[str], *, is_query: bool = False) -> np.ndarray:
        import anyio

        return await anyio.to_thread.run_sync(lambda: self.embed(texts, is_query=is_query))


class OciLlm:
    """`ILlm` を既存の `jetuse_core.genai`（OpenAI 互換 + IAM 署名）で実装する。"""

    def __init__(self) -> None:
        self._cli = make_inference_client()

    def generate(self, prompt, *, response_json_schema=None, **kwargs):
        from oracleagentmemory.apis.llms.llm import LlmResponse

        messages = ([{"role": "user", "content": prompt}] if isinstance(prompt, str)
                    else [dict(m) for m in prompt])
        extra: dict = {}
        if response_json_schema:
            extra["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "out", "schema": response_json_schema, "strict": True},
            }
        r = self._cli.chat.completions.create(
            model=LLM_MODEL, messages=messages, temperature=0, **extra
        )
        return LlmResponse(text=(r.choices[0].message.content or ""))

    async def generate_async(self, prompt, *, response_json_schema=None, **kwargs):
        import anyio

        return await anyio.to_thread.run_sync(
            lambda: self.generate(prompt, response_json_schema=response_json_schema, **kwargs)
        )


def db_pool() -> tuple[str, oracledb.ConnectionPool]:
    """この run 固有スキーマへの接続プール。接頭辞と接続先を確かめてから作る。"""
    common.prepare_env()  # ウォレット取得 → ADB_WALLET_DIR / ADB_DSN / 認可先を env へ
    schema = common.resolve_schema()
    if not schema.startswith(ALLOWED_SCHEMA_PREFIX):
        sys.exit(f"このスパイクが触ってよいのは {ALLOWED_SCHEMA_PREFIX}* だけ"
                 f"（実際: {schema!r}）。中止。")
    common.SCHEMA = schema
    # 接続先が承認済みコンパートメントの loop ADB か・自分が作ったスキーマかを DDL の前に確かめる
    guard = common.connect_schema()
    guard.close()
    secrets = json.loads((common.home() / "secrets.json").read_text())
    wallet = os.environ["ADB_WALLET_DIR"]
    return schema, oracledb.create_pool(
        user=schema, password=secrets["schema_password"], dsn=os.environ["ADB_DSN"],
        config_dir=wallet, wallet_location=wallet,
        wallet_password=secrets.get("wallet_password") or None, min=1, max=4,
    )


def objects(pool) -> dict:
    with pool.acquire() as conn:
        cur = conn.cursor()
        cur.execute("SELECT object_type, object_name FROM user_objects ORDER BY 1, 2")
        rows = [f"{r[0]}: {r[1]}" for r in cur.fetchall()]
        cur.execute("SELECT job_name, repeat_interval, state FROM user_scheduler_jobs")
        jobs = [list(r) for r in cur.fetchall()]
        cols: dict = {}
        cur.execute("SELECT table_name FROM user_tables WHERE table_name LIKE :p ORDER BY 1",
                    p=f"{STORE_ID}%")
        for (t,) in cur.fetchall():
            c = conn.cursor()
            c.execute("SELECT column_name, data_type FROM user_tab_columns"
                      " WHERE table_name = :t ORDER BY column_id", t=t)
            cols[t] = [f"{a}:{b}" for a, b in c.fetchall()]
    return {"objects": rows, "scheduler_jobs": jobs, "columns": cols}


def counts(pool) -> dict:
    """SDK が作った表だけを数える（この run 固有スキーマの他の表は対象外）。"""
    out = {}
    with pool.acquire() as conn:
        cur = conn.cursor()
        cur.execute("SELECT table_name FROM user_tables WHERE table_name LIKE :p ORDER BY 1",
                    p=f"{STORE_ID}%")
        for (t,) in cur.fetchall():
            c = conn.cursor()
            c.execute(f'SELECT COUNT(*) FROM "{t}"')
            out[t] = c.fetchone()[0]
    return out


def _table(name: str) -> str:
    """SDK が実際に作る表名。`memory_store_id` は **アンダースコア区切り**で連結される
    （廃止予定の `table_name_prefix` は区切り無しだった — 名前が変わるので実測で確かめること）。"""
    return f"{STORE_ID}_{name}"


def _text(hit) -> str:
    return str(getattr(hit, "content", hit))


def _provenance(hit) -> dict:
    """ヒットがどのスレッド由来かを取り出す（会話をまたいだ想起の裏付けに使う）。"""
    rec = getattr(hit, "record", None)
    out = {"content": _text(hit)[:200]}
    for attr in ("thread_id", "user_id", "record_type", "memory_type", "id"):
        if hasattr(rec, attr):
            out[attr] = str(getattr(rec, attr))
    return out


def main() -> int:
    from oracleagentmemory.core.dbschemapolicy import SchemaPolicy
    from oracleagentmemory.core.oracleagentmemory import OracleAgentMemory

    schema, pool = db_pool()
    result: dict = {
        "schema": schema,
        "auth_mode": os.environ.get("AUTH_MODE") or get_settings().auth_mode or "config_file",
        "llm_model": LLM_MODEL,
        "embed_model": embeddings.EMBED_MODEL,
    }
    print(f"schema={schema} auth_mode={result['auth_mode']}", flush=True)
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}", flush=True)
        if not ok:
            failures.append(f"{name}: {detail}")

    memory = OracleAgentMemory(
        connection=pool,
        embedder=OciEmbedder(),
        llm=OciLlm(),
        schema_policy=SchemaPolicy.CREATE_IF_NECESSARY,
        memory_store_id=STORE_ID,
    )
    result["after_init"] = objects(pool)

    # 再実行できるように前回の残りを消す（この run のスキーマにしか触れない）
    thread_a, thread_b = f"{STORE_ID.lower()}-thread-a", f"{STORE_ID.lower()}-thread-b"
    for tid in (thread_a, thread_b):
        try:
            memory.delete_thread(tid)
        except Exception:  # noqa: BLE001 — 無ければ消すものが無いだけ
            pass

    user_a, user_b = "spike-user-a", "spike-user-b"

    # ① 会話 A で覚えさせる
    ta = memory.create_thread(user_id=user_a, thread_id=thread_a)
    ta.add_messages([
        {"role": "user", "content": "私の担当はデータ基盤で、朝は必ずコーヒーを飲みます。"},
        {"role": "assistant", "content": "承知しました。データ基盤ご担当ですね。"},
        {"role": "user", "content": "報告書はいつも箇条書きにしてください。"},
        {"role": "assistant", "content": "以降は箇条書きでまとめます。"},
    ])
    memory.wait_for_memory_extraction(timeout=300)
    after_a = counts(pool)
    result["counts_after_thread_a"] = after_a
    check("抽出: 記憶が 1 件以上できた", after_a.get(_table("MEMORY"), 0) >= 1, str(after_a))

    # ② 会話 B（別スレッド・同じ user）を作り、会話 A 由来の記憶を想起できるか。
    #    会話 B 自身には「箇条書き」の話を一切入れない（B の中から引けたら検証にならない）。
    tb = memory.create_thread(user_id=user_a, thread_id=thread_b)
    tb.add_messages([{"role": "user", "content": "この前の件、報告書のまとめ方をどうしましょう。"}])
    memory.wait_for_memory_extraction(timeout=300)

    def _matches(h) -> bool:
        t = _text(h)
        return "bullet" in t.lower() or "箇条書き" in t

    # `record_types` は**記憶の種別**で絞る。抽出された好みは `preference` になるので、
    # `["memory"]` だけを指定すると 0 件になる（実測。ここは踏みやすい罠）。
    hits_user = memory.search("報告書の書き方の好み", user_id=user_a, max_results=5,
                              record_types=MEMORY_RECORD_TYPES)
    result["recall_user_scope"] = [_provenance(h) for h in hits_user]
    hit = next((h for h in hits_user if _matches(h)), None)
    check("会話をまたいだ想起: user スコープで会話 A 由来の記憶が返る", hit is not None,
          str(result["recall_user_scope"]))
    if hit is not None:
        prov = _provenance(hit).get("thread_id")
        check("その記憶の出所が会話 A である", prov in (thread_a, None), f"thread_id={prov}")

    # 会話 B の「コンテキストカード」（SDK が用意する文脈組み立て）に会話 A 由来の記憶が
    # 入るかは**観測にとどめる**（合否にしない）。想起の要件は上の store スコープ検索で
    # 満たしており、カードは既定の絞り込みが効くため入らないことがある。実装時に
    # どちらを統合経路にするかの判断材料として記録する。
    cards = {}
    for label, kwargs in (
        ("default", {}),
        ("widened", {"max_relevant_results": 10,
                     "min_relevant_results_by_type": {"preference": 2, "fact": 1}}),
    ):
        text = str(getattr(tb.get_context_card(**kwargs), "text", ""))
        cards[label] = {"includes_thread_a_memory":
                        ("bullet" in text.lower() or "箇条書き" in text), "text": text[:1500]}
        print(f"  [observe] context_card({label}) に会話 A 由来の記憶: "
              f"{cards[label]['includes_thread_a_memory']}", flush=True)
    result["context_card_from_thread_b"] = cards

    # ③ 別 subject では想起されない
    hits_other = memory.search("報告書の書き方の好み", user_id=user_b,
                               exact_user_match=True, max_results=5)
    result["recall_other_user"] = [_text(h)[:200] for h in hits_other]
    check("subject 分離: 別 user では 0 件", len(hits_other) == 0,
          str(result["recall_other_user"]))

    # ④ 会話（スレッド）削除で派生記憶も消えるか
    before = counts(pool)
    memory.delete_thread(thread_a)
    memory.delete_thread(thread_b)
    after = counts(pool)
    result["delete_threads"] = {"before": before, "after": after}
    for table in (_table("MEMORY"), _table("MESSAGE"), _table("RECORD_CHUNKS")):
        # 削除前に行があったこと**も**確かめる（空の表が 0 のままなのは検証にならない）
        check(f"削除の伝播: {table} が 削除前 >0 → 削除後 0",
              before.get(table, 0) > 0 and after.get(table, -1) == 0,
              f"{before.get(table)} -> {after.get(table)}")
    hits_after = memory.search("報告書の書き方の好み", user_id=user_a, max_results=5)
    result["recall_after_delete"] = [_text(h)[:200] for h in hits_after]
    check("削除後は想起されない", len(hits_after) == 0, str(result["recall_after_delete"]))

    memory.close()
    pool.close()
    result["failures"] = failures
    out = pathlib.Path(os.environ.get("MEM01_RESULT", "/tmp/mem01-sdk-probe.json"))
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"\nwrote {out}", flush=True)
    if failures:
        print("FAILED:\n  " + "\n  ".join(failures), file=sys.stderr)
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
