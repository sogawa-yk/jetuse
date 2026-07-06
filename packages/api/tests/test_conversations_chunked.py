"""demo 会話のチャンク削除(specs/18 §3.2 手順 4 — fake カーソルで順序と再開を検証)。

実 ADB での「大量 messages + 途中停止 → 再 DELETE で続きから完走」は E2E シナリオで実施。
ここでは (1) messages 先行 → ゼロ確認 → conversations の順序、(2) チャンクごとの commit、
(3) usage_log に一切触れない、(4) 途中失敗後の再実行が残りを消す、を fake で固定する。
"""

import contextlib

import pytest

from jetuse_core import conversations


class FakeDb:
    """messages/conversations の行数だけを模した fake(SQL はパターン照合)。"""

    def __init__(self, messages: int, convs: int, fail_after_chunks: int | None = None):
        self.messages = messages
        self.convs = convs
        self.commits = 0
        self.executed: list[str] = []
        self.fail_after_chunks = fail_after_chunks
        self.chunks_done = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1


class FakeCursor:
    def __init__(self, db: FakeDb):
        self.db = db
        self.rowcount = 0
        self._last = None

    def execute(self, sql, **binds):
        self.db.executed.append(" ".join(sql.split()))
        assert "usage_log" not in sql.lower()  # 保持契約: usage_log に触れない
        n = binds.get("n", 0)
        if "DELETE FROM messages" in sql:
            if (self.db.fail_after_chunks is not None
                    and self.db.chunks_done >= self.db.fail_after_chunks):
                raise RuntimeError("timeout injected")
            take = min(self.db.messages, n)
            self.db.messages -= take
            self.rowcount = take
            if take:
                self.db.chunks_done += 1
        elif "SELECT COUNT(*) FROM messages" in sql:
            self._last = (self.db.messages,)
        elif "DELETE FROM conversations" in sql:
            take = min(self.db.convs, n)
            self.db.convs -= take
            self.rowcount = take

    def fetchone(self):
        return self._last


@pytest.fixture()
def use_fake(monkeypatch):
    def install(db):
        monkeypatch.setattr(conversations, "connect",
                            lambda: contextlib.nullcontext(db))
        return db

    return install


def test_messages_first_then_conversations_in_chunks(use_fake):
    db = use_fake(FakeDb(messages=2500, convs=3))
    out = conversations.delete_demo_conversations("d1", chunk=1000)
    assert out == {"messages": 2500, "conversations": 3}
    assert db.messages == 0 and db.convs == 0
    # チャンクごとに commit(2500 → 3 チャンク + conversations 1 チャンク)
    assert db.commits == 4
    # 順序: messages の全チャンク → ゼロ確認 → conversations
    kinds = [("m" if "DELETE FROM messages" in s else
              "z" if "COUNT(*)" in s else "c") for s in db.executed]
    assert kinds == ["m", "m", "m", "m", "z", "c", "c"]  # 末尾 m/c は rowcount=0 の打ち切り確認


def test_interrupted_chunk_resumes_from_progress(use_fake):
    """チャンク commit により、途中失敗しても進捗が残り再実行が続きから収束する。"""
    db = FakeDb(messages=3000, convs=2, fail_after_chunks=2)
    use_fake(db)
    with pytest.raises(RuntimeError):
        conversations.delete_demo_conversations("d1", chunk=1000)
    assert db.messages == 1000  # 2 チャンク分の進捗が残る(全量ロールバックしない)
    assert db.commits == 2

    db.fail_after_chunks = None
    out = conversations.delete_demo_conversations("d1", chunk=1000)
    assert out["messages"] == 1000 and out["conversations"] == 2  # 続きから完走
    assert db.messages == 0 and db.convs == 0


def test_empty_demo_is_noop(use_fake):
    db = use_fake(FakeDb(messages=0, convs=0))
    out = conversations.delete_demo_conversations("d1")
    assert out == {"messages": 0, "conversations": 0}
    assert db.commits == 0
