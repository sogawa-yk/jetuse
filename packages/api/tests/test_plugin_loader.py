"""コントリビューションローダー(PLG-07)の単体/ルートテスト。

検証する受け入れ条件:
  - /api/usecases・/api/agents がインストール済み定義を合算して返す。
  - レスポンスに出所バッジ(origin / source={plugin_id, plugin_name, version})を含む。
  - 名前衝突の解決規則(組み込み > ユーザー作成 > プラグイン取込)で勝者を 1 件選び、
    ほかを shadowed=True にする。
  - 取込定義が無いとき installed_plugins へ問い合わせない(後方互換・追加 I/O ゼロ)。

DB へは接続しない。ローダーの純関数を直接検証し、ルートは repo/store をスタブして通す。
"""

import pytest
from fastapi.testclient import TestClient

from jetuse_core import usecases as uc_repo
from jetuse_core.plugins import loader, store
from service.main import app

client = TestClient(app)


# --- 純関数: 出所バッジ付与 --------------------------------------------------


def test_attach_badge_plugin_uses_index_name():
    item = {"name": "FAQ", "source_plugin_id": "acme/faq", "source_version": "1.2.0"}
    loader._attach_badge(item, {("acme/faq", "1.2.0"): "FAQ要約"})
    assert item["origin"] == "plugin"
    assert item["source"] == {
        "plugin_id": "acme/faq",
        "plugin_name": "FAQ要約",
        "version": "1.2.0",
    }


def test_attach_badge_plugin_falls_back_to_plugin_id():
    item = {"name": "FAQ", "source_plugin_id": "acme/faq", "source_version": "9.9.9"}
    loader._attach_badge(item, {})  # 索引に該当版が無い
    assert item["source"]["plugin_name"] == "acme/faq"


def test_attach_badge_builtin_and_user_have_no_source():
    builtin = {"name": "要約", "builtin": True}
    user = {"name": "私の要約", "owner_sub": "u1"}
    loader._attach_badge(builtin, {})
    loader._attach_badge(user, {})
    assert builtin["origin"] == "builtin" and "source" not in builtin
    assert user["origin"] == "user" and "source" not in user


# --- 純関数: 名前衝突の解決 --------------------------------------------------


def _mk(name, origin):
    it = {"name": name}
    if origin == "builtin":
        it["builtin"] = True
    elif origin == "plugin":
        it["source_plugin_id"] = "acme/x"
        it["source_version"] = "1.0.0"
    return it


def test_collision_builtin_beats_user_beats_plugin():
    items = [_mk("要約", "plugin"), _mk("要約", "user"), _mk("要約", "builtin")]
    for it in items:
        loader._attach_badge(it, {})
    loader._resolve_collisions(items)
    by_origin = {it["origin"]: it for it in items}
    assert by_origin["builtin"]["shadowed"] is False
    assert by_origin["user"]["shadowed"] is True
    assert by_origin["user"]["shadowed_by"] == "builtin"
    assert by_origin["plugin"]["shadowed"] is True
    assert by_origin["plugin"]["shadowed_by"] == "builtin"


def test_collision_user_beats_plugin_when_no_builtin():
    items = [_mk("レポート", "plugin"), _mk("レポート", "user")]
    for it in items:
        loader._attach_badge(it, {})
    loader._resolve_collisions(items)
    by_origin = {it["origin"]: it for it in items}
    assert by_origin["user"]["shadowed"] is False
    assert by_origin["plugin"]["shadowed"] is True
    assert by_origin["plugin"]["shadowed_by"] == "user"


def test_collision_is_case_and_whitespace_insensitive():
    items = [_mk("  要約 ", "plugin"), _mk("要約", "builtin")]
    for it in items:
        loader._attach_badge(it, {})
    loader._resolve_collisions(items)
    plug = next(it for it in items if it["origin"] == "plugin")
    assert plug["shadowed"] is True


def test_no_collision_keeps_everything_unshadowed():
    items = [_mk("A", "builtin"), _mk("B", "user"), _mk("C", "plugin")]
    for it in items:
        loader._attach_badge(it, {})
    loader._resolve_collisions(items)
    assert all(it["shadowed"] is False for it in items)


def test_blank_names_are_not_treated_as_collisions():
    items = [_mk("", "user"), _mk("", "plugin")]
    for it in items:
        loader._attach_badge(it, {})
    loader._resolve_collisions(items)
    assert all(it["shadowed"] is False for it in items)


# --- _enrich_list: 取込が無ければ installed_plugins を引かない -----------------


def test_enrich_list_skips_store_when_no_plugin_items(monkeypatch):
    def boom():
        raise AssertionError("取込定義が無いのに installed_plugins を引いた")

    monkeypatch.setattr(store, "list_installs", boom)
    items = [_mk("要約", "builtin"), _mk("私の", "user")]
    loader._enrich_list(items)  # store を呼ばず完了する
    assert items[0]["origin"] == "builtin"
    assert items[1]["origin"] == "user"


def test_enrich_list_queries_store_once_when_plugin_present(monkeypatch):
    calls = []

    def fake_list_installs():
        calls.append(1)
        return [{"plugin_id": "acme/x", "version": "1.0.0",
                 "manifest": {"name": "エックス"}}]

    monkeypatch.setattr(store, "list_installs", fake_list_installs)
    items = [_mk("要約", "builtin"), _mk("取込UC", "plugin")]
    loader._enrich_list(items)
    assert len(calls) == 1  # 合算で 1 回だけ
    plug = next(it for it in items if it["origin"] == "plugin")
    assert plug["source"]["plugin_name"] == "エックス"


# --- enrich_one: 単一取得の出所バッジ ----------------------------------------


def test_enrich_one_plugin_resolves_name(monkeypatch):
    monkeypatch.setattr(
        store, "find_install",
        lambda pid, ver: {"manifest": {"name": "FAQ要約"}},
    )
    item = {"name": "x", "source_plugin_id": "acme/faq", "source_version": "1.2.0"}
    loader.enrich_one(item)
    assert item["origin"] == "plugin"
    assert item["source"]["plugin_name"] == "FAQ要約"


def test_enrich_one_non_plugin_does_not_query_store(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("通常定義で installed_plugins を引いた")

    monkeypatch.setattr(store, "find_install", boom)
    item = {"name": "x", "builtin": True}
    out = loader.enrich_one(item)
    assert out["origin"] == "builtin" and "source" not in out


def test_enrich_one_none_passthrough():
    assert loader.enrich_one(None) is None


# --- ルート結合: /api/usecases が合算・出所・衝突解決を返す --------------------


@pytest.fixture()
def merged_usecases(monkeypatch):
    """repo を組み込み+ユーザー+取込(衝突あり)に差し替え、store は名前索引を返す。"""
    rows = [
        {"id": "builtin-summarize", "name": "要約", "builtin": True},
        {"id": "u1", "name": "私の分析", "owner_sub": "dev", "builtin": False},
        {"id": "p1", "name": "FAQ要約", "builtin": False,
         "source_plugin_id": "acme/faq", "source_version": "1.2.0"},
        # 組み込みと名前衝突する取込定義(組み込みが勝つ想定)
        {"id": "p2", "name": "要約", "builtin": False,
         "source_plugin_id": "acme/sum", "source_version": "2.0.0"},
    ]
    monkeypatch.setattr(uc_repo, "list_usecases", lambda owner: [dict(r) for r in rows])
    monkeypatch.setattr(
        store, "list_installs",
        lambda: [
            {"plugin_id": "acme/faq", "version": "1.2.0",
             "manifest": {"name": "FAQ プラグイン"}},
            {"plugin_id": "acme/sum", "version": "2.0.0",
             "manifest": {"name": "要約プラグイン"}},
        ],
    )
    return rows


def test_api_usecases_merges_with_source_and_collision(merged_usecases):
    res = client.get("/api/usecases")
    assert res.status_code == 200
    ucs = {u["id"]: u for u in res.json()["usecases"]}

    # 組み込み/ユーザー/プラグインが等価に合算される。
    assert ucs["builtin-summarize"]["origin"] == "builtin"
    assert ucs["u1"]["origin"] == "user"
    assert ucs["p1"]["origin"] == "plugin"

    # 出所バッジ(plugin名/版)。
    assert ucs["p1"]["source"] == {
        "plugin_id": "acme/faq",
        "plugin_name": "FAQ プラグイン",
        "version": "1.2.0",
    }

    # 名前衝突: 組み込み「要約」が勝ち、同名の取込 p2 は shadowed。
    assert ucs["builtin-summarize"]["shadowed"] is False
    assert ucs["p2"]["shadowed"] is True
    assert ucs["p2"]["shadowed_by"] == "builtin"
    # 衝突しない定義は shadowed=False。
    assert ucs["p1"]["shadowed"] is False
    assert ucs["u1"]["shadowed"] is False


def test_api_usecase_get_attaches_source_badge(monkeypatch):
    monkeypatch.setattr(
        uc_repo, "get_usecase",
        lambda owner, uc_id: {
            "id": uc_id, "name": "FAQ要約", "owner_sub": "dev",
            "fields": [], "template": "{{x}}",
            "source_plugin_id": "acme/faq", "source_version": "1.2.0",
        },
    )
    monkeypatch.setattr(
        store, "find_install",
        lambda pid, ver: {"manifest": {"name": "FAQ プラグイン"}},
    )
    got = client.get("/api/usecases/p1").json()
    assert got["origin"] == "plugin"
    assert got["source"]["plugin_name"] == "FAQ プラグイン"
    assert got["source"]["version"] == "1.2.0"


# --- ルート結合: /api/agents も同様に合算・出所を返す ------------------------


def test_api_agents_merges_with_source(monkeypatch):
    from jetuse_core import agents as agents_repo

    rows = [
        {"id": "a1", "name": "私のエージェント", "owner_sub": "dev", "mine": True},
        {"id": "ap1", "name": "ヘルパー", "owner_sub": "dev", "mine": True,
         "source_plugin_id": "acme/helper", "source_version": "1.0.0"},
    ]
    monkeypatch.setattr(agents_repo, "list_agents", lambda owner: [dict(r) for r in rows])
    monkeypatch.setattr(
        store, "list_installs",
        lambda: [{"plugin_id": "acme/helper", "version": "1.0.0",
                  "manifest": {"name": "ヘルパープラグイン"}}],
    )
    agents = {a["id"]: a for a in client.get("/api/agents").json()["agents"]}
    assert agents["a1"]["origin"] == "user"
    assert agents["ap1"]["origin"] == "plugin"
    assert agents["ap1"]["source"]["plugin_name"] == "ヘルパープラグイン"
