"""マイグレーションランナー(CHAT-02)。

jetuse_core/migrations/*.sql を辞書順に適用し、SCHEMA_MIGRATIONS に記録する。
実行: python -m jetuse_core.migrate  (JETUSE_APPユーザーで接続)
SQLファイルは ';' 終端の単文の並び(PL/SQLブロック非対応の簡易版)。
"""

import logging
import pathlib
from collections.abc import Iterable

from .db import connect, get_pool

MIGRATIONS_DIR = pathlib.Path(__file__).parent / "migrations"

logger = logging.getLogger("jetuse.migrate")

# 警告に並べる版の数。全部並べると本文が流れて読まれない。
_SHOW = 5


def _statements(sql: str) -> list[str]:
    return [s.strip() for s in sql.split(";") if s.strip()]


def checkout_versions() -> list[str]:
    """いまの checkout が持つ migration の版(ファイル名の stem)。"""
    return sorted(f.stem for f in MIGRATIONS_DIR.glob("*.sql"))


def applied_versions() -> list[str]:
    """DB に適用済みの版。`schema_migrations` がまだ無ければ空を返す。

    migration を**適用せずに**状態だけ読む。`/api/health` から呼ぶための入口
    (診断のために DDL を流してしまっては本末転倒)。

    接続は `db.connect()`(= `call_timeout` 付き)を使う。`migrate()` 側が生の
    `get_pool().acquire()` なのは適用に時間がかかるためで、**診断はその例外に
    乗せてはいけない** —— ADB 停止時に `/api/health` 自体が返らなくなる(CHAT-07)。
    """
    with connect() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) FROM user_tables WHERE table_name = 'SCHEMA_MIGRATIONS'
        """)
        if cur.fetchone()[0] == 0:
            return []
        cur.execute("SELECT version FROM schema_migrations")
        return sorted(r[0] for r in cur.fetchall())


def pending_versions(db: Iterable[str], checkout: Iterable[str]) -> list[str]:
    """この checkout(コンテナ内ではイメージ)が持っているのに、DB へ適用されていない版。

    **ER-0015 を検出するのはこちら。** 実害時の状態は「DB = Public 集合 / 配備したのは
    Internal 版」だった。DB と migration ランナーの checkout だけを見比べても差は 0 で、
    どちらの側にも「この環境は Internal を要求している」と書いていない。
    判断材料は**動いているアプリのイメージ**にしかない —— イメージが持つ migration が
    DB に無ければ、そのアプリは必要な表の無い DB に向いている。
    """
    return sorted(set(checkout) - set(db))


def foreign_versions(db: Iterable[str], checkout: Iterable[str]) -> list[str]:
    """DB に適用済みだが、いまの checkout に**存在しない**版。

    `pending_versions` とは別の取り違えを見る。こちらが空でないなら、その DB は
    いまの checkout より多くの migration を持つ系統(例: Internal)で育てられている。
    Public 系の checkout から流しても不足分は作られないので、警告して止める材料になる。

    **これだけでは ER-0015 は塞がらない**(実害時はこの差も 0 だった)。塞ぐのは
    `pending_versions` を `/api/health` から見る経路。
    """
    return sorted(set(db) - set(checkout))


def foreign_warning(db: Iterable[str], checkout: Iterable[str]) -> str | None:
    """`foreign_versions` を人が読める警告文にする。該当が無ければ None。

    **言い切れることだけを書く。** ここで分かるのは「DB のほうが先を行っている」まで。
    この checkout が持つ版はすべて DB にあるので、**壊れているとは限らない**
    (後方互換な旧イメージを先行 DB に向ける運用は成り立つ)。不足を断定できるのは
    `pending_versions` を見たときだけなので、503 には言及しない。
    """
    extra = foreign_versions(db, checkout)
    if not extra:
        return None
    shown = ", ".join(extra[:_SHOW]) + (" ほか" if len(extra) > _SHOW else "")
    # 端末に出る文字列なので Markdown の強調記号は使わない(そのまま `**` が見える)。
    return (
        f"この DB には、いまの checkout に無い migration が {len(extra)} 件適用されている"
        f"({shown})。DB のほうが先を行っており、系統の取り違え"
        "(例: Internal で育てたスキーマを Public 系の checkout から流している)か、"
        "意図的に古いイメージを向けているかのどちらか。"
        "配備しているイメージとこの checkout が対応しているか確かめること。"
        "Internal 機能を使う環境なら internal 系の checkout から流し直す。"
    )


def migrate() -> list[str]:
    applied: list[str] = []
    pool = get_pool()
    with pool.acquire() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) FROM user_tables WHERE table_name = 'SCHEMA_MIGRATIONS'
        """)
        if cur.fetchone()[0] == 0:
            cur.execute("""
                CREATE TABLE schema_migrations (
                  version VARCHAR2(64) PRIMARY KEY,
                  applied_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL
                )
            """)
        cur.execute("SELECT version FROM schema_migrations")
        done = {r[0] for r in cur.fetchall()}
        # 適用の前に出す。適用が 0 件で終わったときこそ読ませたい警告なので、
        # 「何かを適用したときだけ何か出る」経路に置いてはいけない。
        warning = foreign_warning(done, checkout_versions())
        if warning:
            logger.warning("%s", warning)
        for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = f.stem
            if version in done:
                continue
            for stmt in _statements(f.read_text()):
                cur.execute(stmt)
            cur.execute(
                "INSERT INTO schema_migrations(version) VALUES (:v)", v=version
            )
            conn.commit()
            applied.append(version)
    return applied


if __name__ == "__main__":
    # 人が読む経路。`jetuse_core.logging.configure()` の JSON ではなく素の1行で出す
    # (警告を見落とすと ER-0015 の「流したのに動かない」に戻る)。
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    done = migrate()
    # **「up to date」と言い切らない。** 別系統の checkout から流すと 0 件で正常終了するため、
    # 0 件を「最新」と断定すると ER-0015 の誤解をこの行が再生産する。
    # 判断材料は直前の WARNING に出る。
    print(f"applied: {done or '(none)'}")
