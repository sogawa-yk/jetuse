"""migration の取り違えに気づけることを固定する(ER-0015)。

**なぜテストが要るか**: 2026-08-04、Internal 版を配備したのに個人スキーマへ Internal 固有
migration が適用されておらず、`GET /api/demos` が 503 になった。**どこも失敗していない**——
配備は成功し、アプリは起動し、`migrate` を流しても「適用 0 件」で正常終了する。
沈黙が症状なので、**「何も起きない」ことを検査する**必要がある。

見る対象が2つある。混同すると片方しか塞げない。

| | 差の向き | 何が分かるか |
|---|---|---|
| `pending_versions` | イメージ − DB | **配備したアプリが要求する表が DB に無い**（実害） |
| `foreign_versions` | DB − checkout | いま流す checkout が、その DB の系統として足りない |

実害時は **DB も checkout も Public 集合**だったので、`foreign_versions` は 0 だった。
`foreign_versions` だけでは塞がらない、というのがこのテストの中心的な主張。

**集合は checkout から推定しない。** `PUBLIC = checkout_versions()` と書くと、
`internal-dev` では checkout が既に Internal 固有版を含むため `PUBLIC + INTERNAL_ONLY` の
差が空になり、このファイル全体が Internal 側で壊れる。ランナーが読むディレクトリごと
差し替えて、どちらのブランチでも同じことを検査する。
"""

from __future__ import annotations

import contextlib
import logging
import pathlib
import subprocess
import sys
import textwrap

import pytest

import jetuse_core.migrate as mig

API_DIR = pathlib.Path(mig.__file__).resolve().parents[1]

# 2026-08-04 に実際に踏んだ形。internal-dev だけが持つ 11 本。
INTERNAL_ONLY = [
    "017_demos_v2",
    "018_demos_idx_owner",
    "019_demos_idx_visibility",
    "020_conversations_demo_id",
    "021_conversations_idx_demo",
    "022_demo_backend_targets",
    "023_dbt_idx",
    "024_rag_files_filename_char",
    "025_builder_sessions",
    "026_builder_sessions_idx",
    "027_builder_sessions_sufficient",
]

# Public 版が持つ集合。**実行中の checkout に依存させない**ため素の一覧で持つ。
PUBLIC = ["001_init", "002_presets", "016_demos", "021_http_tool_headers"]
INTERNAL = [*PUBLIC, *INTERNAL_ONLY]


@pytest.fixture
def image(monkeypatch, tmp_path):
    """ランナーが読む `migrations/` を差し替える（＝配備イメージの中身を決める）。"""

    def _set(versions: list[str]) -> pathlib.Path:
        d = tmp_path / "migrations"
        d.mkdir(exist_ok=True)
        for v in versions:
            (d / f"{v}.sql").write_text(f"CREATE TABLE t_{v.replace('-', '_')} (id NUMBER);")
        monkeypatch.setattr(mig, "MIGRATIONS_DIR", d)
        return d

    return _set


# --- ER-0015 の実害そのもの ---------------------------------------------------


def test_the_real_failure_is_an_image_that_needs_more_than_the_db_has():
    """**実害の状態**: DB は Public 集合、配備したのは Internal 版。11 件が不足。"""
    assert mig.pending_versions(PUBLIC, INTERNAL) == sorted(INTERNAL_ONLY)


def test_foreign_versions_alone_would_not_have_caught_it():
    """**逆向きの差では塞がらない。**

    実害時は DB も migration ランナーの checkout も Public 集合だった。
    どちらの側にも「この環境は Internal を要求している」と書かれていないので、
    この2つを見比べる限り差は 0 になる。ここが 0 でも検出できる経路が要る、
    というのが `pending_versions` を足した理由。
    """
    assert mig.foreign_versions(PUBLIC, PUBLIC) == []
    assert mig.pending_versions(PUBLIC, INTERNAL), "実害を検出できる側まで 0 になっている"


def test_nothing_pending_when_the_db_matches_the_image():
    assert mig.pending_versions(INTERNAL, INTERNAL) == []
    assert mig.pending_versions(PUBLIC, PUBLIC) == []


def test_fresh_database_has_everything_pending():
    assert mig.pending_versions([], PUBLIC) == sorted(PUBLIC)


def test_a_db_ahead_of_the_image_has_nothing_pending():
    """DB のほうが先行していても「未適用」にはしない(そちらは foreign 側の話)。"""
    assert mig.pending_versions(INTERNAL, PUBLIC) == []
    assert mig.foreign_versions(INTERNAL, PUBLIC) == sorted(INTERNAL_ONLY)


# --- checkout の取り違え(ランナー側) ------------------------------------------


def test_nothing_foreign_when_db_is_a_subset_of_the_checkout():
    assert mig.foreign_versions(["001_init", "002_presets"], PUBLIC) == []
    assert mig.foreign_warning(["001_init", "002_presets"], PUBLIC) is None


def test_fresh_database_is_not_foreign():
    assert mig.foreign_versions([], PUBLIC) == []


def test_internal_schema_seen_from_a_public_checkout_is_detected():
    assert mig.foreign_versions(INTERNAL, PUBLIC) == sorted(INTERNAL_ONLY)


def test_warning_names_the_count_and_examples():
    msg = mig.foreign_warning(INTERNAL, PUBLIC)
    assert msg is not None
    assert f"{len(INTERNAL_ONLY)} 件" in msg
    assert "017_demos_v2" in msg          # 先頭は必ず出す
    assert "ほか" in msg                   # 5 本を超えるので省略を明示する
    assert "internal" in msg               # 次の一手が書いてある
    assert "**" not in msg, "端末に出す文字列に Markdown の強調記号が混ざっている"


def test_warning_does_not_claim_the_app_is_broken():
    """**言い切れないことを書かない。**

    DB が先行しているだけなら、この checkout が要求する版はすべて DB にある。
    後方互換な旧イメージを先行 DB に向ける運用は成り立つので、
    「表が足りない」「503 のまま」と断定してはいけない。
    """
    msg = mig.foreign_warning(INTERNAL, PUBLIC)
    assert msg is not None
    assert "503" not in msg
    assert "不足" not in msg


def test_warning_lists_everything_when_few():
    msg = mig.foreign_warning(["900_x", "901_y"], ["001_init"])
    assert msg is not None
    assert "900_x" in msg and "901_y" in msg
    assert "ほか" not in msg, "全部並べたのに省略を示唆している"


def test_checkout_versions_reads_real_files_without_extension():
    """実ファイルを読む部分だけは本物を見る（ブランチによらず成り立つ性質のみ）。"""
    versions = mig.checkout_versions()
    assert versions, "migrations が1つも読めていない"
    assert versions == sorted(versions)
    assert all(not v.endswith(".sql") for v in versions), "stem ではなくファイル名を返している"
    assert "001_init" in versions


def test_expectations_do_not_depend_on_the_current_branch():
    """**このファイルは public / internal のどちらの checkout でも同じ結論を出す。**

    `PUBLIC` を `checkout_versions()` から作ると、internal 系では Internal 固有版が
    既に含まれるため差が消え、Internal 側でだけテストが壊れる（sync のたびに CI が落ちる）。
    """
    real = set(mig.checkout_versions())
    assert not (set(PUBLIC) & set(INTERNAL_ONLY)), "定数どうしが重なっている"
    assert mig.pending_versions(PUBLIC, INTERNAL) == sorted(INTERNAL_ONLY), (
        f"checkout({len(real)} 本)に引きずられている"
    )


# --- fake プール ---------------------------------------------------------------


class _FakeCursor:
    """`test_plugin_store.py` の fake と同じ約束(実 DDL は no-op)。"""

    def __init__(self, state: dict):
        self.state = state
        self._result: list[tuple] = []

    def execute(self, sql: str, **binds):
        s = " ".join(sql.split())
        if "FROM user_tables" in s and "SCHEMA_MIGRATIONS" in s:
            self._result = [(1 if self.state["created"] else 0,)]
        elif s.startswith("CREATE TABLE schema_migrations"):
            self.state["created"] = True
        elif s.startswith("SELECT version FROM schema_migrations"):
            self._result = [(v,) for v in sorted(self.state["applied"])]
        elif s.startswith("INSERT INTO schema_migrations"):
            self.state["applied"].add(binds["v"])
        else:
            self.state["ddl"].append(s)

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)


class _FakeConn:
    def __init__(self, state: dict):
        self.state = state
        self.call_timeout: int | None = None

    def cursor(self):
        return _FakeCursor(self.state)

    def commit(self):
        pass


class _FakePool:
    def __init__(self, state: dict):
        self.state = state
        self.conns: list[_FakeConn] = []

    @contextlib.contextmanager
    def acquire(self):
        conn = _FakeConn(self.state)
        self.conns.append(conn)
        yield conn


def _state(applied: list[str], *, created: bool = True) -> dict:
    return {"created": created, "applied": set(applied), "ddl": []}


@pytest.fixture
def pool(monkeypatch):
    """`migrate` と `db.connect` の両方が同じ fake を掴むようにする。"""

    def _set(applied: list[str], *, created: bool = True) -> _FakePool:
        p = _FakePool(_state(applied, created=created))
        monkeypatch.setattr(mig, "get_pool", lambda: p)
        import jetuse_core.db as db

        monkeypatch.setattr(db, "get_pool", lambda: p)
        return p

    return _set


# --- 状態だけ読む入口 ----------------------------------------------------------


def test_applied_versions_reads_without_applying_anything(pool):
    """**診断のために DDL を流さない。** health から呼ぶので副作用があってはならない。"""
    p = pool(["001_init", "002_presets"])

    assert mig.applied_versions() == ["001_init", "002_presets"]
    assert p.state["ddl"] == [], "読むだけのはずが DDL を流している"


def test_applied_versions_on_a_schema_without_the_table(pool):
    """`schema_migrations` がまだ無い DB でも落ちず、空を返す(作りもしない)。"""
    p = pool([], created=False)

    assert mig.applied_versions() == []
    assert p.state["created"] is False, "診断が表を作ってしまっている"


def test_applied_versions_sets_a_call_timeout(pool):
    """**ADB 停止時に `/api/health` ごと固まらない。**

    `db.connect()` は SQL 往復に `call_timeout` を張る契約(CHAT-07)。診断経路が
    生の `acquire()` を使うとこれを迂回し、DB が止まっている**まさにその時**に
    診断が返らなくなる。
    """
    import jetuse_core.db as db

    p = pool(["001_init"])
    mig.applied_versions()

    assert p.conns, "接続を取っていない"
    assert p.conns[-1].call_timeout == db.CALL_TIMEOUT_MS


# --- ランナーの警告 ------------------------------------------------------------


def test_warns_even_though_nothing_is_applied(pool, image, caplog):
    """適用 0 件で終わる経路でこそ警告が要る(沈黙が症状だった)。"""
    image(PUBLIC)
    pool(INTERNAL)

    with caplog.at_level(logging.WARNING, logger="jetuse.migrate"):
        applied = mig.migrate()

    assert applied == [], "前提が崩れている(このケースは適用 0 件のはず)"
    assert caplog.records, "適用 0 件のときに何も言わない = ER-0015 の再発"
    assert f"{len(INTERNAL_ONLY)} 件" in caplog.records[0].getMessage()


def test_no_warning_on_a_normal_run(pool, image, caplog):
    """正常時に警告を出さない。毎回出ると本当に見るべきときに読まれなくなる。"""
    image(PUBLIC)
    pool([])

    with caplog.at_level(logging.WARNING, logger="jetuse.migrate"):
        applied = mig.migrate()

    assert applied == sorted(PUBLIC), "前提が崩れている(fresh なら全部適用されるはず)"
    assert not caplog.records


def test_warning_precedes_any_ddl(pool, image, monkeypatch):
    """警告は適用の前に出す。落ちた後に出しても手遅れになる。"""
    image(PUBLIC)
    p = pool([PUBLIC[0], *INTERNAL_ONLY])
    seen: list[str] = []
    monkeypatch.setattr(mig.logger, "warning",
                        lambda *a, **k: seen.append(f"warn:{len(p.state['ddl'])}"))

    mig.migrate()
    assert seen == ["warn:0"], "DDL を流した後に警告している"


def test_idempotent_second_run_still_warns(pool, image, caplog):
    """2 回目以降も黙らない。1 回目を見落としたら永久に分からない、では困る。"""
    image(PUBLIC)
    pool(INTERNAL)

    with caplog.at_level(logging.WARNING, logger="jetuse.migrate"):
        mig.migrate()
        mig.migrate()

    assert len(caplog.records) == 2


# --- CLI(人の目に届くか) ------------------------------------------------------


# どのブランチの `migrations/` にも存在しない版。CLI 検査で DB 側に足して使う。
ABSENT_EVERYWHERE = [f"9{n:02d}_absent_everywhere" for n in range(11)]

CLI_DRIVER = """
import contextlib
import pathlib
import runpy

import jetuse_core.db as db

# `runpy` は module を**作り直す**ので、事前に import して属性を書き換えても効かない。
# そこで DB 側だけを操作する: 実ファイルの版すべて + どのブランチにも存在しない版。
# → pending = 0（適用 0 件で終わる）/ foreign = 足した分、がブランチによらず成り立つ。
MIG_DIR = pathlib.Path(db.__file__).parent / "migrations"
VERSIONS = sorted(f.stem for f in MIG_DIR.glob("*.sql")) + {extra!r}


class C:
    def __init__(s, v):
        s.v = v
        s._r = []

    def execute(s, sql, **b):
        t = " ".join(sql.split())
        if "FROM user_tables" in t:
            s._r = [(1,)]
        elif t.startswith("SELECT version FROM schema_migrations"):
            s._r = [(x,) for x in s.v]
        else:
            s._r = []

    def fetchone(s):
        return s._r[0] if s._r else None

    def fetchall(s):
        return list(s._r)


class Conn:
    def __init__(s, v):
        s.v = v

    def cursor(s):
        return C(s.v)

    def commit(s):
        pass


class Pool:
    def __init__(s, v):
        s.v = v

    def acquire(s):
        @contextlib.contextmanager
        def _cm():
            yield Conn(s.v)

        return _cm()


db.get_pool = lambda: Pool(VERSIONS)
runpy.run_module("jetuse_core.migrate", run_name="__main__")
"""


def test_cli_prints_the_warning_and_does_not_claim_up_to_date(tmp_path):
    """**人が読む経路**で警告が出て、かつ「最新」と嘘をつかないこと。

    `migrate()` が logger に出していても、CLI でログ設定が無ければ人には届かない。
    プロセスを起こして stderr を確かめる。
    """
    driver = tmp_path / "drive_cli.py"
    driver.write_text(textwrap.dedent(CLI_DRIVER).format(extra=ABSENT_EVERYWHERE))
    r = subprocess.run([sys.executable, str(driver)], cwd=str(API_DIR),
                       capture_output=True, text=True, timeout=120)

    assert r.returncode == 0, f"CLI が落ちた:\n{r.stderr}"
    assert f"{len(ABSENT_EVERYWHERE)} 件" in r.stderr, f"警告が人に届いていない:\n{r.stderr}"
    assert "up to date" not in r.stdout, "適用 0 件を「最新」と言い切っている"
    assert "applied: (none)" in r.stdout


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_versions_are_not_treated_as_foreign(blank):
    """空文字が混ざっても黙って捨てない。`schema_migrations` の空 version は異常なので、
    「余っている」と正直に数えるほうが安全。"""
    assert mig.foreign_versions([blank], ["001_init"]) == [blank]
