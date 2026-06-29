"""コア同梱 sample-app SBA-B「在庫・受発注照会」(SBA-03 / NL2SQL)。

SBA-A(問い合わせ管理)で確立した型(`kind: sample-app` の manifest + `validate_sample_app` /
`validate_composition` 準拠)に倣い、**自然言語で業務DBを照会し結果をグラフ化する**業務アプリの
リファレンス実装。SBA-02 の AI 組込フレームワーク(`ai_runtime`)が `nl2sql`(自然言語DB照会)と
`chart`(結果グラフ化)を実行時バインドする。

データモデル(datasets):
  - `inventory`: 在庫(商品マスタ＋在庫数/発注点)。倉庫・カテゴリ別の在庫照会の対象。
  - `orders`   : 受発注明細(受注=売上 / 発注=仕入)。期間・取引先・商品別の集計照会の対象。

AI 組込スロット(aiSlots):
  - `nl2sql-query` → nl2sql : 日本語の照会から読取専用 SELECT を生成(SQL-02 ガード流用)。
  - `result-chart` → chart  : 実行結果に最適なグラフ仕様を提案(既存 Chart で描画)。

照会の**実行**は読取専用ユーザー(JETUSE_QUERY)経由の既存ガード(SELECT 限定・行数上限・
タイムアウト= SQL-02 / specs/10-dbchat.md)をそのまま流用する。datasets はその対象 DB スキーマ
(実環境 E2E では JETUSE_SBA03)へ展開され、生成 SQL はテーブル名(大文字)で参照する。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from .manifest import PluginManifest, validate_manifest
from .sample_app import SampleAppDefinition, validate_sample_app

#: コア同梱 SBA-B の固定 ID(プラグイン取込と衝突しない 'builtin-' 規約)。
SBA_B_ID = "jetuse/inventory-orders"
SBA_B_INSTANCE_ID = "builtin-sba-b"
# 実環境 E2E の展開先スキーマ(例 JETUSE_SBA03)は環境依存値のためコードに固定しない。
# .env / E2E 手順(docs/verification/SBA-03.md, runs/<id>/e2e/run_e2e.py)側で管理する。


def _inv(
    code: str, name: str, category: str, warehouse: str,
    qty: int, price: int, reorder: int, updated: str,
) -> dict[str, Any]:
    """在庫 1 行。"""
    return {
        "product_code": code, "product_name": name, "category": category,
        "warehouse": warehouse, "quantity": qty, "unit_price": price,
        "reorder_point": reorder, "updated_at": updated,
    }


# --- 在庫(inventory)シード ----------------------------------------------
# quantity < reorder_point の品目(P-1002/1008/1010/1012/1014)は「発注点割れ」照会で拾える。
_INVENTORY_SEED: list[dict[str, Any]] = [
    _inv("P-1001", "コピー用紙A4 500枚", "事務用品", "東京DC", 320, 480, 100, "2026-06-20"),
    _inv("P-1002", "油性ボールペン 黒", "事務用品", "東京DC", 60, 90, 150, "2026-06-22"),
    _inv("P-1003", "ステープラー 中型", "事務用品", "大阪DC", 140, 650, 40, "2026-06-18"),
    _inv("P-1004", "ミネラルウォーター 500ml", "飲料", "大阪DC", 880, 88, 300, "2026-06-24"),
    _inv("P-1005", "緑茶 ペットボトル 525ml", "飲料", "福岡DC", 210, 120, 250, "2026-06-24"),
    _inv("P-1006", "ドリップコーヒー 50P", "飲料", "東京DC", 95, 320, 80, "2026-06-19"),
    _inv("P-1007", "インスタント味噌汁 20食", "食品", "大阪DC", 150, 150, 60, "2026-06-15"),
    _inv("P-1008", "レトルトカレー 中辛", "食品", "福岡DC", 48, 210, 100, "2026-06-21"),
    _inv("P-1009", "アルカリ乾電池 単3 8本", "日用品", "東京DC", 260, 280, 120, "2026-06-17"),
    _inv("P-1010", "トイレットペーパー 12R", "日用品", "大阪DC", 70, 360, 90, "2026-06-23"),
    _inv("P-1011", "ウェットティッシュ 80枚", "日用品", "福岡DC", 330, 198, 100, "2026-06-20"),
    _inv("P-1012", "USBメモリ 32GB", "電子機器", "東京DC", 45, 980, 50, "2026-06-16"),
    _inv("P-1013", "HDMIケーブル 2m", "電子機器", "大阪DC", 120, 740, 40, "2026-06-22"),
    _inv("P-1014", "LED電球 60W相当", "電子機器", "福岡DC", 30, 520, 60, "2026-06-25"),
    _inv("P-1015", "付箋 ふせんセット", "事務用品", "東京DC", 410, 240, 120, "2026-06-14"),
]


def _order(
    oid: str, date: str, otype: str, code: str, name: str, partner: str,
    qty: int, price: int, status: str, delivery: str,
) -> dict[str, Any]:
    """受発注 1 行。金額(amount)は数量×単価で一貫させる(集計照会の検算に使える)。"""
    return {
        "order_id": oid, "order_date": date, "order_type": otype,
        "product_code": code, "product_name": name, "partner": partner,
        "quantity": qty, "unit_price": price, "amount": qty * price,
        "status": status, "delivery_date": delivery,
    }


# --- 受発注(orders)シード ------------------------------------------------
# order_type: 受注=売上(顧客向け出荷) / 発注=仕入(仕入先からの入荷)。期間は 2026-01〜06。
_ORDER_SEED: list[dict[str, Any]] = [
    _order("SO-2601", "2026-01-12", "受注", "P-1001", "コピー用紙A4 500枚",
           "山田商事", 120, 480, "shipped", "2026-01-18"),
    _order("SO-2602", "2026-01-25", "受注", "P-1004", "ミネラルウォーター 500ml",
           "みらい物産", 600, 88, "shipped", "2026-01-30"),
    _order("PO-2601", "2026-01-15", "発注", "P-1012", "USBメモリ 32GB",
           "テック仕入", 100, 920, "received", "2026-01-22"),
    _order("SO-2603", "2026-02-03", "受注", "P-1006", "ドリップコーヒー 50P",
           "さくら流通", 80, 320, "shipped", "2026-02-08"),
    _order("SO-2604", "2026-02-14", "受注", "P-1010", "トイレットペーパー 12R",
           "山田商事", 200, 360, "shipped", "2026-02-19"),
    _order("PO-2602", "2026-02-18", "発注", "P-1004", "ミネラルウォーター 500ml",
           "大阪飲料卸", 1000, 70, "received", "2026-02-25"),
    _order("SO-2605", "2026-03-02", "受注", "P-1013", "HDMIケーブル 2m",
           "あおぞら電器", 60, 740, "shipped", "2026-03-07"),
    _order("SO-2606", "2026-03-11", "受注", "P-1001", "コピー用紙A4 500枚",
           "みらい物産", 150, 480, "shipped", "2026-03-16"),
    _order("SO-2607", "2026-03-22", "受注", "P-1008", "レトルトカレー 中辛",
           "さくら流通", 240, 210, "shipped", "2026-03-27"),
    _order("PO-2603", "2026-03-25", "発注", "P-1014", "LED電球 60W相当",
           "ひかり照明", 200, 410, "received", "2026-04-01"),
    _order("SO-2608", "2026-04-05", "受注", "P-1005", "緑茶 ペットボトル 525ml",
           "山田商事", 480, 120, "shipped", "2026-04-10"),
    _order("SO-2609", "2026-04-17", "受注", "P-1012", "USBメモリ 32GB",
           "あおぞら電器", 40, 980, "shipped", "2026-04-22"),
    _order("SO-2610", "2026-04-28", "受注", "P-1009", "アルカリ乾電池 単3 8本",
           "みらい物産", 130, 280, "shipped", "2026-05-03"),
    _order("PO-2604", "2026-04-30", "発注", "P-1010", "トイレットペーパー 12R",
           "日用品センター", 300, 300, "received", "2026-05-07"),
    _order("SO-2611", "2026-05-09", "受注", "P-1001", "コピー用紙A4 500枚",
           "さくら流通", 180, 480, "shipped", "2026-05-14"),
    _order("SO-2612", "2026-05-20", "受注", "P-1006", "ドリップコーヒー 50P",
           "山田商事", 110, 320, "shipped", "2026-05-25"),
    _order("SO-2613", "2026-05-26", "受注", "P-1013", "HDMIケーブル 2m",
           "テック仕入", 90, 740, "open", "2026-06-02"),
    _order("PO-2605", "2026-05-29", "発注", "P-1006", "ドリップコーヒー 50P",
           "珈琲問屋", 150, 250, "received", "2026-06-05"),
    _order("SO-2614", "2026-06-04", "受注", "P-1004", "ミネラルウォーター 500ml",
           "みらい物産", 720, 88, "shipped", "2026-06-09"),
    _order("SO-2615", "2026-06-12", "受注", "P-1014", "LED電球 60W相当",
           "あおぞら電器", 70, 520, "open", "2026-06-19"),
    _order("SO-2616", "2026-06-18", "受注", "P-1001", "コピー用紙A4 500枚",
           "山田商事", 160, 480, "open", "2026-06-24"),
    _order("SO-2617", "2026-06-23", "受注", "P-1008", "レトルトカレー 中辛",
           "さくら流通", 200, 210, "open", "2026-06-30"),
    _order("PO-2606", "2026-06-24", "発注", "P-1012", "USBメモリ 32GB",
           "テック仕入", 80, 920, "open", "2026-07-01"),
    _order("SO-2618", "2026-06-25", "受注", "P-1010", "トイレットペーパー 12R",
           "日用品センター", 150, 360, "open", "2026-07-02"),
]

_DEFINITION: dict[str, Any] = {
    "summary": "在庫・受発注照会の業務アプリ。自然言語の質問から読取専用 SQL を生成し(NL2SQL)、"
    "在庫数・受発注金額を実行・集計して結果をグラフ化する(SBA-B)。SELECT 限定・行数上限・"
    "タイムアウトの読取専用ガード(SQL-02)を流用し、業務DBを安全に照会するリファレンス実装。",
    "datasets": [
        {
            "name": "inventory",
            "label": "在庫",
            "fields": [
                {"name": "product_code", "type": "string", "label": "商品コード", "required": True},
                {"name": "product_name", "type": "string", "label": "商品名", "required": True},
                {"name": "category", "type": "string", "label": "カテゴリ"},
                {"name": "warehouse", "type": "string", "label": "倉庫"},
                {"name": "quantity", "type": "number", "label": "在庫数"},
                {"name": "unit_price", "type": "number", "label": "単価"},
                {"name": "reorder_point", "type": "number", "label": "発注点"},
                {"name": "updated_at", "type": "date", "label": "更新日"},
            ],
            "seed": _INVENTORY_SEED,
        },
        {
            "name": "orders",
            "label": "受発注",
            "fields": [
                {"name": "order_id", "type": "string", "label": "伝票番号", "required": True},
                {"name": "order_date", "type": "date", "label": "伝票日付"},
                {"name": "order_type", "type": "string", "label": "区分"},
                {"name": "product_code", "type": "string", "label": "商品コード"},
                {"name": "product_name", "type": "string", "label": "商品名"},
                {"name": "partner", "type": "string", "label": "取引先"},
                {"name": "quantity", "type": "number", "label": "数量"},
                {"name": "unit_price", "type": "number", "label": "単価"},
                {"name": "amount", "type": "number", "label": "金額"},
                {"name": "status", "type": "string", "label": "ステータス"},
                {"name": "delivery_date", "type": "date", "label": "納期"},
            ],
            "seed": _ORDER_SEED,
        },
    ],
    "aiSlots": [
        {
            "key": "nl2sql-query",
            "title": "自然言語DB照会",
            "capability": "nl2sql",
            "permissions": ["platform:db.query"],
        },
        {"key": "result-chart", "title": "結果グラフ化", "capability": "chart"},
    ],
    "screens": [
        {
            "key": "inventory",
            "title": "在庫一覧",
            "type": "list",
            "dataset": "inventory",
        },
        {
            "key": "orders",
            "title": "受発注一覧",
            "type": "list",
            "dataset": "orders",
        },
        {
            "key": "query",
            "title": "AI照会コンソール",
            "type": "dashboard",
            "slots": ["nl2sql-query", "result-chart"],
        },
    ],
}

_MANIFEST: dict[str, Any] = {
    "schemaVersion": "1",
    "id": SBA_B_ID,
    # 1.1.0: permissions に platform:connector.invoke を追加した版(方式A / ADR-0020 D7)。版固定
    # スナップショットのため版を繰り上げ、旧 1.0.0 install 済み環境でも新権限契約を再インストール・
    # 再承認できるようにする(BLK-001)。
    "version": "1.1.0",
    "kind": "sample-app",
    "name": "在庫・受発注照会",
    "description": "自然言語DB照会(NL2SQL)＋結果グラフ化を備えた在庫・受発注デモ(SBA-B)。",
    "publisher": "jetuse",
    "jetuse": {"minVersion": "0.3.0"},
    # platform:db.query は NL2SQL slot 由来。platform:connector.invoke は Slack 等のコネクタを
    # 束ねて通知する消費デモとして invoke を呼ぶ権利の宣言(方式A / ADR-0020 D7)。宣言は invoke を
    # 承認可能にするだけで、実 grant は配備が実際に Slack を束縛したときに限り invoke を含む。
    "permissions": ["platform:db.query", "platform:connector.invoke"],
    "contributes": {"sample-app": _DEFINITION},
    "tags": ["inventory", "orders", "nl2sql", "chart", "sample-app"],
    "icon": "📦",
}


@lru_cache(maxsize=1)
def sba_b_manifest() -> PluginManifest:
    """検証済みの SBA-B manifest(kind=sample-app)。"""
    return validate_manifest(_MANIFEST)


@lru_cache(maxsize=1)
def sba_b_definition() -> SampleAppDefinition:
    """検証済みの SBA-B sample-app 定義(screens/datasets/aiSlots)。"""
    return validate_sample_app(sba_b_manifest())


def sba_b_summary() -> dict[str, Any]:
    """home/実行導線が一覧表示するための SBA-B 要約。"""
    m = sba_b_manifest()
    d = sba_b_definition()
    return {
        "id": SBA_B_INSTANCE_ID,
        "plugin_id": m.id,
        "version": m.version,
        "name": m.name,
        "description": m.description,
        "icon": m.icon or "📦",
        "tags": list(m.tags),
        "builtin": True,
        "capabilities": sorted({s.capability for s in d.ai_slots}),
        "screens": [s.key for s in d.screens],
    }


def get_sba_b_sample_app(app_id: str) -> dict[str, Any] | None:
    """app_id から SBA-B の完全定義(seed 含む)を返す。無ければ None。

    公開 API のパス(`/api/sample-apps/{app_id}`)で扱う ID は URL-safe な instance id
    (`builtin-sba-b`)に統一する(SBA-A と同方針。plugin_id は slash を含み path にマッチしない)。
    SBA-B は NL2SQL のため RAG 知識コーパスを持たない(knowledge_dataset=None)。
    """
    if app_id != SBA_B_INSTANCE_ID:
        return None
    m = sba_b_manifest()
    d = sba_b_definition()
    return {
        "id": SBA_B_INSTANCE_ID,
        "plugin_id": m.id,
        "version": m.version,
        "name": m.name,
        "description": m.description,
        "icon": m.icon or "📦",
        "tags": list(m.tags),
        "builtin": True,
        "knowledge_dataset": None,
        "definition": d.model_dump(by_alias=True),
    }
