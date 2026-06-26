"""マイグレーション SQL 分割の健全性テスト(HBD-01 回帰防止)。

実 DB を使わず、素朴な ';' 分割器がコメント内のセミコロンで壊れない(017 のコメント '; ' 回帰防止)
ことを確認する。冪等性は migrate の version スキップ＋実環境 E2E(deploy.log の2回適用)で担保。
"""

import pathlib

from jetuse_core import migrate

MIGRATIONS = pathlib.Path(migrate.__file__).parent / "migrations"


def _non_comment(stmt: str) -> str:
    return "\n".join(
        ln for ln in stmt.splitlines() if not ln.strip().startswith("--")
    ).strip()


def test_all_migrations_split_into_executable_statements():
    """どの migration も分割後の各フラグメントが『コメントのみ』にならず実 SQL を含む。

    コメント内に ';' があるとコメント断片や壊れた文片が execute され migration が失敗するため、
    その回帰を全 migration に対して防ぐ。
    """
    for path in sorted(MIGRATIONS.glob("*.sql")):
        for frag in migrate._statements(path.read_text()):
            assert _non_comment(frag), f"{path.name}: コメントのみのフラグメントを検出"


def test_017_parses_to_create_statements():
    stmts = migrate._statements((MIGRATIONS / "017_hearing.sql").read_text())
    heads = [_non_comment(s).split()[0].upper() for s in stmts]
    # 3 テーブル + 1 索引(一意制約はインライン)。全て CREATE。
    assert heads == ["CREATE", "CREATE", "CREATE", "CREATE"]
