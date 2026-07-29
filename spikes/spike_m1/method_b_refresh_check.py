"""②-8 「$VECTAB へ足した列」が索引リフレッシュ後も使い物になるかの実機確認。

方式②で任意メタデータを持たせる唯一の道は「$VECTAB に列を足して自分で埋める」だが、
索引はバケットから定期同期される。同期で入った新しい行に自前列の値は入らないはずで、
その場合「補完処理を別途回し続ける必要がある」という運用コストが方式②に付く。
推測で書かず実際に走らせて確かめる。

実行: PYTHONPATH=spikes/spike_m1 .venv/bin/python spikes/spike_m1/method_b_refresh_check.py
"""

import time

import oracledb

from common import banner, connect_spike, require_owned_bucket, require_owned_schema
from method_b_select_ai import BUCKET, PREFIX, VECTAB, _os_client

PIPELINE = "JETUSE_SPIKE_M1_IDX$VECPIPELINE"
NEW_TEXT = "在庫棚卸API GET /v2/stocktakes は棚卸結果の一覧を返す。期間指定は最大31日である。"


def next_probe_name(cur: oracledb.Cursor) -> tuple[str, str]:
    """まだ索引に無いチャンク名を選ぶ。

    既に索引済みの名前を使うと「新規行が入らない → NULL 0 件 → 欠損なし」と
    誤読する（実際に一度そうなった）。必ず新規オブジェクトで測る。
    """
    cur.execute(f"SELECT JSON_VALUE(attributes, '$.object_name') FROM \"{VECTAB}\"")
    known = {r[0] for r in cur.fetchall() if r[0]}
    for i in range(11, 100):
        stem = f"c{i}__v2.0__current__spec"
        if f"{stem}.txt" not in known:
            return f"{PREFIX}{stem}.txt", stem
    raise RuntimeError("未使用のプローブ名が見つからない")


def counts(cur: oracledb.Cursor) -> list[tuple]:
    cur.execute(f'SELECT NVL(current_version, \'(NULL)\'), COUNT(*) '
                f'FROM "{VECTAB}" GROUP BY current_version')
    return sorted(cur.fetchall())


def main() -> None:
    conn = connect_spike()
    require_owned_schema(conn)   # 既存パイプラインを STOP/START する前に所有確認
    cur = conn.cursor()
    banner("②-8 索引リフレッシュ後、自前で足した列 current_version はどうなるか")
    print("  リフレッシュ前:", counts(cur))

    cli = _os_client()
    ns = cli.get_namespace().data
    require_owned_bucket(cli.get_bucket(ns, BUCKET).data.id, BUCKET)  # 他人のバケットへ触らない
    new_obj, stem = next_probe_name(cur)
    cli.put_object(ns, BUCKET, new_obj, NEW_TEXT.encode("utf-8"))
    print(f"  新規オブジェクト追加: {new_obj}（索引に未登録のものを選択）")
    try:
        _measure(conn, cur, cli, ns, new_obj, stem)
    finally:
        # 途中でどこが落ちてもプローブは残さない（残すと後続のレイテンシ計測を汚す）
        cleanup_probe(conn, cur, cli, ns, new_obj, stem)
    conn.close()


def cleanup_probe(conn, cur, cli, ns, new_obj, stem) -> None:
    failed = []
    try:
        cli.delete_object(ns, BUCKET, new_obj)
    except Exception as e:  # noqa: BLE001 - 後始末は続行しつつ結果を必ず出す
        print(f"  後始末 NG（オブジェクト）: {type(e).__name__}")
        failed.append("object")
    try:
        cur.execute(f'DELETE FROM "{VECTAB}" '
                    "WHERE JSON_VALUE(attributes, '$.object_name') = :n", n=f"{stem}.txt")
        conn.commit()
        cur.execute(f'SELECT COUNT(*) FROM "{VECTAB}"')
        print(f"  後始末: {new_obj} とその索引行を削除 → $VECTAB {cur.fetchone()[0]} 行"
              "（10 件の基準セットへ戻る）")
    except Exception as e:  # noqa: BLE001
        print(f"  後始末 NG（索引行）: {type(e).__name__}")
        failed.append("vectab")
    if failed:
        # 残したまま成功終了すると後続のレイテンシ計測を汚した状態で「完了」になる
        raise RuntimeError(f"プローブの後始末に失敗した: {failed}")


def _measure(conn, cur, cli, ns, new_obj, stem) -> None:

    # 実機で判明: 走行中のパイプラインは前景実行できない（ORA-20044）。停止→1回実行→再開する。
    def run(label: str, stmt: str) -> None:
        try:
            cur.execute(stmt)
            print(f"  {label} -> OK")
        except oracledb.DatabaseError as e:
            # 握り潰すと「同期されなかったから NULL 0 件」を「欠損なし」と誤読する
            print(f"  {label} -> NG（エラー全文）:")
            print("     " + str(e).replace("\n", "\n     "))
            raise

    run("STOP_PIPELINE", f"BEGIN DBMS_CLOUD_PIPELINE.STOP_PIPELINE('{PIPELINE}'); END;")
    try:
        run("RUN_PIPELINE_ONCE",
            f"BEGIN DBMS_CLOUD_PIPELINE.RUN_PIPELINE_ONCE('{PIPELINE}'); END;")
    finally:
        # 途中で失敗してもパイプラインを停止したまま放置しない
        run("START_PIPELINE", f"BEGIN DBMS_CLOUD_PIPELINE.START_PIPELINE('{PIPELINE}'); END;")

    ingested = 0
    for _ in range(36):  # 最大 3 分待つ
        time.sleep(5)
        cur.execute(f"SELECT COUNT(*) FROM \"{VECTAB}\" "
                    "WHERE JSON_VALUE(attributes, '$.object_name') = :n", n=f"{stem}.txt")
        ingested = cur.fetchone()[0]
        if ingested:
            break
    if not ingested:
        # 同期されていないのに「欠損なし」と読むのは誤り。判定不能として落とす
        raise RuntimeError(f"新規オブジェクト {stem} が索引に入らなかった。この検証は成立しない")
    cur.execute(f'SELECT COUNT(*) FROM "{VECTAB}"')
    print("  リフレッシュ後の総行数:", cur.fetchone()[0])
    print("  リフレッシュ後の current_version 内訳:", counts(cur))
    # 表全体の NULL 件数ではなく、**今回入ったプローブ行**の値だけを見る
    # （過去の失敗で NULL 行が残っていると誤判定するため）。
    cur.execute(f'SELECT NVL(current_version, \'(NULL)\') FROM "{VECTAB}" '
                "WHERE JSON_VALUE(attributes, '$.object_name') = :n", n=f"{stem}.txt")
    probe_value = [r[0] for r in cur.fetchall()]
    print(f"  今回のプローブ行 {stem}.txt の current_version: {probe_value}")
    if probe_value == ["(NULL)"]:
        print("  => 同期で入った行の current_version は NULL。"
              "方式②では取り込みのたびに自前の補完処理を回し続ける必要がある")
    else:
        raise RuntimeError(f"想定外: プローブ行の値が {probe_value}（NULL を期待）。"
                           " ②の結論（同期行はメタ欠損）が成立していない")


if __name__ == "__main__":
    main()
