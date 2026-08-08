"""`/api/health` が「DB がイメージに追いついていない」を報告することを固定する(ER-0015)。

**なぜここで見るか**: migration が流れるかどうかは配備経路で違う。ORM ワンクリック配備は
`RUN_DB_BOOTSTRAP=true` で自動適用するが、開発者ごとのスタック(`ops/dev-env-up.sh`)は
流さない。流れない経路で配備すると、アプリは**必要な表が無い DB に向いたまま正常起動し**、
DB を使う機能だけが 503 になる。2026-08-04 はこれで原因に辿り着けなかった。

DB と migration ランナーの checkout を見比べても分からない。どちらにも「この環境が何を
要求しているか」が書かれていないからだ。書いてあるのは**動いているイメージが持つ
migration の一覧**で、それが答えになる。
"""

from __future__ import annotations

import pytest

from jetuse_core import health
from jetuse_core import migrate as mig

# **実行中の checkout から推定しない。** `internal-dev` では `checkout_versions()` が
# 既に Internal 固有版を含むため、そこから作ると差が消えて Internal 側でだけ壊れる
# （sync のたびに CI が落ちる）。素の一覧で持ち、イメージ側も必ず monkeypatch する。
PUBLIC = ["001_init", "002_presets", "016_demos", "021_http_tool_headers"]
INTERNAL_ONLY = [
    "017_demos_v2", "018_demos_idx_owner", "019_demos_idx_visibility",
    "020_conversations_demo_id", "021_conversations_idx_demo",
    "022_demo_backend_targets", "023_dbt_idx", "024_rag_files_filename_char",
    "025_builder_sessions", "026_builder_sessions_idx",
    "027_builder_sessions_sufficient",
]
INTERNAL = [*PUBLIC, *INTERNAL_ONLY]


@pytest.fixture
def env(monkeypatch):
    """DB に適用済みの版と、動いているイメージが持つ版の**両方**を決める。"""

    def _set(applied: list[str] | Exception, image: list[str] = PUBLIC):
        def _read():
            if isinstance(applied, Exception):
                raise applied
            return sorted(applied)

        monkeypatch.setattr(mig, "applied_versions", _read)
        monkeypatch.setattr(mig, "checkout_versions", lambda: sorted(image))

    return _set


@pytest.fixture
def all_capabilities_ok(monkeypatch):
    """スキーマ以外を全部 ok にして、`ok` への影響だけを見る。"""
    monkeypatch.setattr(health, "_rag_health", lambda: {"status": "ok"})
    for name in ("chat_health", "dbchat_health", "speech_health", "ocr_health",
                 "tts_health", "agents_health"):
        monkeypatch.setattr(health, name, lambda: {"status": "ok"})


def test_behind_when_the_image_needs_more_than_the_db_has(env):
    """**ER-0015 の実害そのもの。** Internal 版を配備し、DB は Public 集合のまま。"""
    env(PUBLIC, image=INTERNAL)

    out = health.schema_health()
    assert out["status"] == "behind"
    assert out["pending"] == sorted(INTERNAL_ONLY)
    assert "11 件未適用" in out["hint"]
    assert "503" in out["hint"], "症状から引けない"
    assert "dev-environments" in out["hint"], "次の一手が書かれていない"


def test_hint_does_not_overstate_the_impact(env):
    """**「未適用がある」と「だから壊れている」を混ぜない。**

    未適用がインデックス追加だけなら機能は動く（実際、Internal 固有 11 本のうち
    3 本は index）。断定した診断を信じると、動いているものを壊れていると思って
    別のところを探すことになる。
    """
    env(PUBLIC, image=INTERNAL)

    hint = health.schema_health()["hint"]
    assert "表を使う機能は 503 になる" in hint, "条件付きの言い方になっていない"
    assert "DB を使う機能は 503 のままになる" not in hint, "影響を断定している"


def test_behind_flips_the_overall_ok(env, all_capabilities_ok):
    """未適用があるのに ok=true と言わない。**その嘘が ER-0015 で人を迷わせた。**"""
    env(PUBLIC, image=INTERNAL)

    out = health.capability_health()
    assert out["schema"]["status"] == "behind"
    assert out["ok"] is False
    # capability ではなく前提条件なので capabilities には混ぜない。
    assert "schema" not in out["capabilities"]


def test_ok_when_the_db_matches_the_image(env, all_capabilities_ok):
    env(PUBLIC, image=PUBLIC)

    out = health.capability_health()
    assert out["schema"]["status"] == "ok"
    assert out["schema"]["applied"] == len(PUBLIC)
    assert out["schema"]["expected"] == len(PUBLIC)
    assert out["ok"] is True


def test_foreign_when_the_db_is_ahead(env):
    """DB のほうが先を行っている。壊れてはいないので ok は落とさない。"""
    env(INTERNAL, image=PUBLIC)

    out = health.schema_health()
    assert out["status"] == "foreign"
    assert out["foreign"] == sorted(INTERNAL_ONLY)
    assert "別系統" in out["hint"]


def test_foreign_does_not_flip_the_overall_ok(env, all_capabilities_ok):
    env(INTERNAL, image=PUBLIC)

    assert health.capability_health()["ok"] is True


def test_behind_wins_over_foreign(env):
    """両方あるときは「未適用」を出す。壊れている側が先に読まれるべき。"""
    env([*PUBLIC, "800_db_only"], image=[*PUBLIC, "900_image_only"])

    out = health.schema_health()
    assert out["status"] == "behind"
    assert out["pending"] == ["900_image_only"]


def test_db_unreachable_is_unknown_not_ok(env):
    """DB を読めないことを「問題なし」に丸めない。"""
    env(RuntimeError("no wallet source"))

    out = health.schema_health()
    assert out["status"] == "unknown"
    assert "RuntimeError" in out["hint"]


def test_unknown_does_not_flip_the_overall_ok(env, all_capabilities_ok):
    """DB 未接続は他の capability が既に報告しているので、ここで二重に落とさない。"""
    env(RuntimeError("boom"))

    assert health.capability_health()["ok"] is True


def test_health_check_does_not_apply_migrations(env, monkeypatch):
    """診断が DDL を流さない。健康診断が状態を変えては話にならない。"""
    called = []
    monkeypatch.setattr(mig, "migrate", lambda: called.append("migrated") or [])
    env(PUBLIC, image=PUBLIC)

    health.schema_health()
    assert called == []


def test_fresh_schema_reports_every_version_pending(env):
    """まだ何も適用していない DB は、全件が未適用として出る(空 = 正常ではない)。"""
    env([], image=PUBLIC)

    out = health.schema_health()
    assert out["status"] == "behind"
    assert out["applied"] == 0
    assert len(out["pending"]) == len(PUBLIC)


def test_expectations_do_not_depend_on_the_current_branch(env):
    """**public / internal のどちらの checkout でも同じ結論を出す。**

    `PUBLIC` を `checkout_versions()` から作ると、internal 系では Internal 固有版が
    既に含まれるため差が消え、Internal 側でだけ壊れる。
    """
    assert not (set(PUBLIC) & set(INTERNAL_ONLY)), "定数どうしが重なっている"
    env(PUBLIC, image=INTERNAL)
    assert health.schema_health()["pending"] == sorted(INTERNAL_ONLY)
