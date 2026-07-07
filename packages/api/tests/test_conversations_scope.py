"""会話リポジトリの demo_id スコープ契約(SP2-03 / specs/18 §4.2)。

user 単位の全 verb が SQL レベルで `demo_id IS NULL` を強制し、demo スコープは
exact 一致で照合することを、SQL を捕捉する fake 接続で検証する(実 ADB は E2E)。
"""

import contextlib

import pytest

from jetuse_core import conversations as conv_repo


class _Cur:
    def __init__(self, log):
        self.log = log
        self.rowcount = 1

    def execute(self, sql, **binds):
        self.log.append((" ".join(sql.split()), binds))

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class _Conn:
    def __init__(self, log):
        self.log = log

    def cursor(self):
        return _Cur(self.log)

    def commit(self):
        pass


@pytest.fixture()
def sql_log(monkeypatch):
    log: list[tuple[str, dict]] = []

    @contextlib.contextmanager
    def fake_connect():
        yield _Conn(log)

    monkeypatch.setattr(conv_repo, "connect", fake_connect)
    return log


def _sql(log, i=0):
    return log[i][0].upper()


def test_user_verbs_enforce_demo_id_is_null(sql_log):
    """一覧/GET/DELETE/title/set_oci の全 verb(specs/18 §4.2 — 両方向の持ち込み 404)。"""
    conv_repo.list_conversations("u")
    conv_repo.get_conversation("u", "c1")
    conv_repo.delete_conversation("u", "c1")
    conv_repo.update_title("u", "c1", "t")
    conv_repo.set_oci_conversation("u", "c1", "oc")
    assert len(sql_log) == 5
    for sql, _ in sql_log:
        assert "DEMO_ID IS NULL" in sql.upper(), sql


def test_demo_scope_get_uses_exact_match(sql_log):
    conv_repo.get_conversation("u", "c1", "d1")
    sql, binds = sql_log[0]
    assert "DEMO_ID = :D" in sql.upper()
    assert binds["d"] == "d1"
    assert "IS NULL" not in sql.upper()


def test_create_conversation_binds_demo_id(sql_log):
    conv_repo.create_conversation("u", "gpt-oss-120b", None, demo_id="d1")
    sql, binds = sql_log[0]
    assert "DEMO_ID" in sql.upper()
    assert binds["d"] == "d1"
    # user 経路(demo_id 省略)は NULL で INSERT(既存挙動と互換)
    conv_repo.create_conversation("u", "gpt-oss-120b", None)
    assert sql_log[1][1]["d"] is None
