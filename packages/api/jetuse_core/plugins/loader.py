"""コントリビューションローダー(PLG-07)。

組み込み / ユーザー作成 / インストール済み(プラグイン取込)の定義を **等価に** 既存
エンジンへ統合する層。取込定義は PLG-03 の installer が usecases/agents 表へ
`source_plugin_id`/`source_version` を刻んで版固定で書き込むため、リポジトリの
`list_usecases`/`list_agents` は既に合算済みの一覧を返す。本ローダーはその一覧に対し
次の 2 点を付与・適用する:

  1. **出所バッジ**: 取込定義に `origin="plugin"` と `source={plugin_id, plugin_name,
     version}` を付ける。plugin_name は installed_plugins(manifest)から引く(無ければ
     plugin_id にフォールバック)。組み込みは `origin="builtin"`、それ以外は
     `origin="user"`。
  2. **名前衝突の解決**: 表示名(`name`)が同じ定義が複数あるとき、優先順位
     組み込み > ユーザー作成 > プラグイン取込 で勝者を 1 件選び、ほかを `shadowed=True`
     にする(`shadowed_by` に勝者の origin)。勝者だけが正準(`shadowed=False`)。

設計判断(優先順位の根拠): 組み込みは安定 ID/キュレーション済みで最優先。次にこの環境の
運用者自身が作った定義。第三者スナップショットであるプラグイン取込が組み込み/ユーザー定義を
隠してはならないので最下位。同順位内は元の並び順(更新日時降順)を保ち、決定的に解決する。

リポジトリの薄いラッパとして実装し、DB 問い合わせは「取込定義が一覧に含まれるとき」だけ
追加で 1 回行う(installed_plugins の名前索引)。取込定義が無い通常運用では追加 I/O ゼロで、
既存 API の後方互換を壊さない。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from .. import agents as agents_repo
from .. import usecases as usecases_repo
from . import store

# 名前衝突の優先順位(小さいほど優先)。組み込み > ユーザー作成 > プラグイン取込。
_ORIGIN_PRIORITY = {"builtin": 0, "user": 1, "plugin": 2}


def _origin_of(item: dict[str, Any]) -> str:
    """定義の出所種別を判定する。builtin > plugin > user。"""
    if item.get("builtin"):
        return "builtin"
    if item.get("source_plugin_id"):
        return "plugin"
    return "user"


def _plugin_name_index() -> dict[tuple[str, str], str]:
    """(plugin_id, version) -> プラグイン表示名 の索引を installed_plugins から作る。

    manifest が壊れている/名前が無い記録は索引に載せない(呼び出し側が plugin_id に
    フォールバックする)。一覧合算で 1 回だけ呼ぶ。
    """
    index: dict[tuple[str, str], str] = {}
    for rec in store.list_installs():
        manifest = rec.get("manifest") or {}
        name = manifest.get("name")
        if name:
            index[(rec["plugin_id"], rec["version"])] = name
    return index


def _attach_badge(item: dict[str, Any], index: dict[tuple[str, str], str]) -> None:
    """1 定義に origin と(取込定義なら)出所バッジ source を付ける。"""
    origin = _origin_of(item)
    item["origin"] = origin
    if origin == "plugin":
        pid = item.get("source_plugin_id")
        ver = item.get("source_version")
        item["source"] = {
            "plugin_id": pid,
            "version": ver,
            "plugin_name": index.get((pid, ver)) or pid,
        }


def _resolve_collisions(items: list[dict[str, Any]]) -> None:
    """表示名の衝突を優先順位規則で解決し、各定義に shadowed フラグを立てる。

    名前(前後空白除去・大小無視)でグループ化し、優先順位最上位を勝者にする。同順位は
    元の並び順で最初の 1 件(min は安定)。勝者は shadowed=False、ほかは shadowed=True +
    shadowed_by=勝者の origin。名前が空の定義は衝突解決の対象外(各々 shadowed=False)。
    """
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for it in items:
        key = (it.get("name") or "").strip().casefold()
        groups[key].append(it)
    for key, group in groups.items():
        if not key or len(group) < 2:
            for it in group:
                it.setdefault("shadowed", False)
            continue
        winner = min(group, key=lambda it: _ORIGIN_PRIORITY[it["origin"]])
        for it in group:
            if it is winner:
                it["shadowed"] = False
            else:
                it["shadowed"] = True
                it["shadowed_by"] = winner["origin"]


def _enrich_list(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """合算済み一覧に出所バッジを付け、名前衝突を解決して返す(破壊的・同一リスト)。"""
    has_plugin = any(it.get("source_plugin_id") for it in items)
    index = _plugin_name_index() if has_plugin else {}
    for it in items:
        _attach_badge(it, index)
    _resolve_collisions(items)
    return items


def list_usecases(owner: str) -> list[dict[str, Any]]:
    """組み込み + ユーザー + インストール済みのユースケースを合算し、出所付きで返す。"""
    return _enrich_list(usecases_repo.list_usecases(owner))


def list_agents(owner: str) -> list[dict[str, Any]]:
    """組み込み(無し) + ユーザー + インストール済みのエージェントを合算し、出所付きで返す。"""
    return _enrich_list(agents_repo.list_agents(owner))


def enrich_one(item: dict[str, Any] | None) -> dict[str, Any] | None:
    """単一定義(get_usecase/get_agent の戻り)に出所バッジを付ける。

    取込定義のときだけ installed_plugins を 1 件引いて plugin_name を解決する
    (通常定義・組み込みでは追加 I/O なし)。
    """
    if item is None:
        return None
    origin = _origin_of(item)
    item["origin"] = origin
    if origin == "plugin":
        pid = item.get("source_plugin_id")
        ver = item.get("source_version")
        name = None
        if pid and ver:
            rec = store.find_install(pid, ver)
            if rec and rec.get("manifest"):
                name = rec["manifest"].get("name")
        item["source"] = {
            "plugin_id": pid,
            "version": ver,
            "plugin_name": name or pid,
        }
    return item
