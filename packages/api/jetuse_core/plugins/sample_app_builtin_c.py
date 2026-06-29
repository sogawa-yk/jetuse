"""コア同梱 sample-app SBA-C「営業案件管理(SFA-lite)」(SBA-04)。

SBA-A(サポートデスク)と同じ型(コード同梱・`kind: sample-app` manifest・aiSlots による
AI 組込)で、営業案件の業務フロー(パイプライン → 案件コンソール → 売上分析)に **複合AI** を
連動させたリファレンス業務アプリ。AGT/VOICE/NL2SQL の既存能力を組込点に流用する:

  - `minutes-summary` → minutes : 商談議事録(生メモ/発言録)の構造化要約(VOICE-01 流用)
  - `next-actions`    → agent   : 案件情報＋議事録要約から次アクションを提案(AGT 系の宣言型流用)
  - `sales-rollup`    → nl2sql  : 自然言語の売上集計を専用スキーマ JETUSE_SBA04 へ照会(NL2SQL 流用)
  - `email-draft`     → draft   : 顧客向けフォローメールの下書き(返信ドラフトを流用。実送信はしない)

「連動」= 議事録要約の出力を次アクション提案へ、案件情報＋売上集計＋次アクションをメール下書きへ
渡す(UI/E2E がチェーンする)。売上集計だけは実 ADB の専用スキーマ(JETUSE_SBA04)を照会し、
他の3能力は GenAI 推論のみで動く。

データモデル(datasets):
  - `deals`    : 営業案件(パイプライン)。stage/amount/probability/owner/close_date。
  - `meetings` : 商談議事録(deal_id ごと)。`notes` が minutes 要約の入力。
  - `sales`    : 受注売上明細。NL2SQL 集計対象。E2E では JETUSE_SBA04.SALES へ投入して照会する。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from .manifest import PluginManifest, validate_manifest
from .sample_app import SampleAppDefinition, validate_sample_app

#: コア同梱 SBA-C の固定 ID(instance id と slash 付き plugin_id を分ける規約は SBA-A と同じ)。
SBA_C_ID = "jetuse/sales-deals"
SBA_C_INSTANCE_ID = "builtin-sba-c"

#: 売上集計(nl2sql)が照会する実 DB 専用スキーマ。共有 loop ADB をタスク専用スキーマで隔離する
#: (ADB は増やさない)。E2E セットアップが ADMIN で CREATE USER JETUSE_SBA04 → 案件/売上を投入する。
SBA_C_NL2SQL_SCHEMA = "JETUSE_SBA04"

_DEALS_SEED: list[dict[str, Any]] = [
    {
        "id": "deal-001",
        "name": "山田製作所 — MES連携クラウド導入",
        "customer": "山田製作所株式会社",
        "stage": "提案",
        "amount": 12_000_000,
        "probability": 60,
        "owner": "佐々木",
        "close_date": "2026-08-29",
        "next_step": "PoC範囲の合意",
        "updated_at": "2026-06-24",
    },
    {
        "id": "deal-002",
        "name": "明日工業 — 在庫最適化SaaS",
        "customer": "明日工業株式会社",
        "stage": "交渉",
        "amount": 8_400_000,
        "probability": 75,
        "owner": "小林",
        "close_date": "2026-07-31",
        "next_step": "見積の最終調整",
        "updated_at": "2026-06-25",
    },
    {
        "id": "deal-003",
        "name": "未来システム — データ基盤刷新",
        "customer": "株式会社未来システム",
        "stage": "見積",
        "amount": 21_500_000,
        "probability": 50,
        "owner": "加藤",
        "close_date": "2026-09-30",
        "next_step": "技術検証結果の共有",
        "updated_at": "2026-06-23",
    },
    {
        "id": "deal-004",
        "name": "あおぞら商事 — 受発注EDI",
        "customer": "株式会社あおぞら商事",
        "stage": "受注",
        "amount": 5_600_000,
        "probability": 100,
        "owner": "佐々木",
        "close_date": "2026-06-15",
        "next_step": "キックオフ調整",
        "updated_at": "2026-06-16",
    },
    {
        "id": "deal-005",
        "name": "さくらデザイン — 名刺管理連携",
        "customer": "さくらデザイン事務所",
        "stage": "リード",
        "amount": 1_800_000,
        "probability": 20,
        "owner": "小林",
        "close_date": "2026-10-31",
        "next_step": "初回ヒアリング設定",
        "updated_at": "2026-06-20",
    },
    {
        "id": "deal-006",
        "name": "グローバル物流 — TMS高度化",
        "customer": "グローバル物流株式会社",
        "stage": "提案",
        "amount": 16_200_000,
        "probability": 55,
        "owner": "加藤",
        "close_date": "2026-09-12",
        "next_step": "ROI試算の提示",
        "updated_at": "2026-06-25",
    },
]

#: 議事録(商談メモ)。`notes` が minutes 要約の入力。複合AIの「連動」起点。
_MEETINGS_SEED: list[dict[str, Any]] = [
    {
        "id": "mtg-001",
        "deal_id": "deal-001",
        "title": "山田製作所 第2回提案レビュー",
        "date": "2026-06-24",
        "attendees": "先方: 情報システム部 山田部長, 生産技術 田中 / 当社: 佐々木, 加藤",
        "notes": "山田部長より、現行MESは老朽化し保守切れが近いと共有。クラウド移行には前向きだが、"
        "工場ネットワークからの接続セキュリティと、停止時間を最小化する移行計画が懸念。"
        "田中氏からは既存ラインのPLCデータ連携の実績有無を質問された。価格は予算枠1,500万円程度を"
        "示唆。次回までにPoCの対象ラインを2本に絞り、移行ステップ案とセキュリティ構成図を提示する"
        "ことで合意。決裁は8月の役員会を想定。",
    },
    {
        "id": "mtg-002",
        "deal_id": "deal-002",
        "title": "明日工業 価格交渉",
        "date": "2026-06-25",
        "attendees": "先方: 購買 鈴木課長 / 当社: 小林",
        "notes": "鈴木課長より、他社相見積もりがあり当社提示額は約8%高いと指摘。"
        "年間保守費の内訳開示を"
        "要望された。導入時期は第3四半期を希望。小林より、3年契約での値引きと、初期構築費の分割払い"
        "案を口頭提示。先方は社内稟議のため、正式見積を今週中に欲しいとのこと。値引き上限は10%まで"
        "と本部長承認済み。",
    },
    {
        "id": "mtg-003",
        "deal_id": "deal-003",
        "title": "未来システム 技術検証キックオフ",
        "date": "2026-06-23",
        "attendees": "先方: CTO 伊藤, データ基盤 高橋 / 当社: 加藤, 佐々木",
        "notes": "伊藤CTOより、現行のオンプレDWHはバッチ遅延が常態化し意思決定が遅いと課題提示。"
        "リアルタイム分析基盤への刷新が狙い。高橋氏から既存ETLの本数(約200本)と移行優先度の"
        "整理が必要と指摘。セキュリティ要件として個人情報のマスキングが必須。検証は2週間で主要"
        "ユースケース3件を対象に実施。予算は2,000万円台で、9月期の投資枠を確保済み。",
    },
]

def _sale(
    sid: str, customer: str, product: str, region: str,
    owner: str, amount: int, closed_at: str,
) -> dict[str, Any]:
    """受注売上 1 行を組み立てる(seed 行の桁折れ回避＋型の明示)。

    SALES は自己完結したファクト表(顧客/製品/地域/担当/金額/受注日)。`deal_id` は持たない
    ——アクティブな案件(deals)と過去の受注実績(sales)は別エンティティで、人工的な外部キーで
    結ぶと NL2SQL が不要な JOIN を選び集計を取りこぼすため(E2E で実証)、ファクト表は独立させる。
    """
    return {
        "id": sid, "customer": customer, "product": product,
        "region": region, "owner": owner, "amount": amount, "closed_at": closed_at,
    }


#: 受注売上明細(NL2SQL 集計対象)。E2E では JETUSE_SBA04.SALES へ投入して自然言語照会する。
#: region/owner/product/closed_at 別の集計デモが成立するよう分布させる(担当は deals と同じ3名)。
_SALES_SEED: list[dict[str, Any]] = [
    _sale("s-001", "株式会社あおぞら商事", "受発注EDI", "関東", "佐々木", 5_600_000, "2026-06-15"),
    _sale("s-002", "北日本フーズ", "在庫最適化SaaS", "東北", "小林", 7_200_000, "2026-05-28"),
    _sale("s-003", "関西精密", "MES連携クラウド", "関西", "加藤", 13_400_000, "2026-05-12"),
    _sale("s-004", "東海マテリアル", "データ基盤", "中部", "加藤", 18_900_000, "2026-04-30"),
    _sale("s-005", "九州ロジ", "TMS高度化", "九州", "佐々木", 9_800_000, "2026-06-03"),
    _sale("s-006", "信州システムズ", "在庫最適化SaaS", "中部", "小林", 6_100_000, "2026-05-20"),
    _sale("s-007", "都心リテール", "受発注EDI", "関東", "小林", 4_300_000, "2026-06-09"),
    _sale("s-008", "瀬戸内マニュ", "MES連携クラウド", "中国", "加藤", 11_700_000, "2026-04-18"),
    _sale("s-009", "札幌ネット", "データ基盤", "北海道", "佐々木", 15_200_000, "2026-05-31"),
    _sale("s-010", "横浜トレード", "TMS高度化", "関東", "加藤", 8_600_000, "2026-06-21"),
    _sale("s-011", "名古屋工機", "MES連携クラウド", "中部", "小林", 12_900_000, "2026-06-12"),
    _sale("s-012", "福岡データ", "在庫最適化SaaS", "九州", "佐々木", 5_400_000, "2026-04-25"),
]

_DEFINITION: dict[str, Any] = {
    "summary": "営業案件管理(SFA-lite)業務アプリ。"
    "パイプライン→案件コンソール→売上分析の業務フローに、"
    "議事録要約・次アクション提案エージェント・売上集計(NL2SQL)・メール下書きの複合AIを連動させた"
    "リファレンス業務アプリ(SBA-C)。",
    "datasets": [
        {
            "name": "deals",
            "label": "案件",
            "fields": [
                {"name": "id", "type": "string", "label": "ID"},
                {"name": "name", "type": "string", "label": "案件名", "required": True},
                {"name": "customer", "type": "string", "label": "顧客", "required": True},
                {"name": "stage", "type": "string", "label": "ステージ"},
                {"name": "amount", "type": "number", "label": "金額"},
                {"name": "probability", "type": "number", "label": "確度(%)"},
                {"name": "owner", "type": "string", "label": "担当"},
                {"name": "close_date", "type": "date", "label": "完了予定"},
                {"name": "next_step", "type": "string", "label": "次ステップ"},
                {"name": "updated_at", "type": "date", "label": "更新日"},
            ],
            "seed": _DEALS_SEED,
        },
        {
            "name": "meetings",
            "label": "商談議事録",
            "fields": [
                {"name": "id", "type": "string", "label": "ID"},
                {"name": "deal_id", "type": "string", "label": "案件ID", "required": True},
                {"name": "title", "type": "string", "label": "件名", "required": True},
                {"name": "date", "type": "date", "label": "日付"},
                {"name": "attendees", "type": "string", "label": "出席者"},
                {"name": "notes", "type": "text", "label": "議事メモ", "required": True},
            ],
            "seed": _MEETINGS_SEED,
        },
        {
            "name": "sales",
            "label": "受注売上",
            "fields": [
                {"name": "id", "type": "string", "label": "ID"},
                {"name": "customer", "type": "string", "label": "顧客"},
                {"name": "product", "type": "string", "label": "製品"},
                {"name": "region", "type": "string", "label": "地域"},
                {"name": "owner", "type": "string", "label": "担当"},
                {"name": "amount", "type": "number", "label": "売上額", "required": True},
                {"name": "closed_at", "type": "date", "label": "受注日"},
            ],
            "seed": _SALES_SEED,
        },
    ],
    "aiSlots": [
        {"key": "minutes-summary", "title": "議事録要約", "capability": "minutes"},
        {"key": "next-actions", "title": "次アクション提案", "capability": "agent"},
        {"key": "sales-rollup", "title": "売上集計(NL2SQL)", "capability": "nl2sql"},
        {"key": "email-draft", "title": "フォローメール下書き", "capability": "draft"},
    ],
    "screens": [
        {
            "key": "pipeline",
            "title": "案件パイプライン",
            "type": "list",
            "dataset": "deals",
        },
        {
            "key": "deal",
            "title": "案件コンソール",
            "type": "detail",
            "dataset": "deals",
            "slots": ["minutes-summary", "next-actions", "email-draft"],
        },
        {
            "key": "analytics",
            "title": "売上分析",
            "type": "dashboard",
            "dataset": "sales",
            "slots": ["sales-rollup"],
        },
    ],
}

_MANIFEST: dict[str, Any] = {
    "schemaVersion": "1",
    "id": SBA_C_ID,
    # 1.1.0: permissions に platform:connector.invoke を追加した版(方式A / ADR-0020 D7)。版固定
    # スナップショットのため版を繰り上げ、旧 1.0.0 install 済み環境でも新権限契約を再インストール・
    # 再承認できるようにする(BLK-001)。
    "version": "1.1.0",
    "kind": "sample-app",
    "name": "営業案件管理",
    "description": "議事録要約・次アクション提案・売上集計(NL2SQL)・メール下書きを"
    "連動させた営業案件管理デモ(SBA-C)。",
    "publisher": "jetuse",
    "jetuse": {"minVersion": "0.3.0"},
    # platform:connector.invoke は Slack 等のコネクタを束ねて通知する消費デモとして invoke を呼ぶ
    # 権利の宣言(方式A / ADR-0020 D7)。宣言は invoke を承認可能にするだけで、実 grant は配備が実際に
    # Slack を active 束縛したときに限り invoke を含む(最小権限は grant 段で保つ)。
    "permissions": ["platform:connector.invoke"],
    "contributes": {"sample-app": _DEFINITION},
    "tags": ["sales", "agent", "nl2sql", "sample-app"],
    "icon": "📊",
}


@lru_cache(maxsize=1)
def sba_c_manifest() -> PluginManifest:
    """検証済みの SBA-C manifest(kind=sample-app)。"""
    return validate_manifest(_MANIFEST)


@lru_cache(maxsize=1)
def sba_c_definition() -> SampleAppDefinition:
    """検証済みの SBA-C sample-app 定義(screens/datasets/aiSlots)。"""
    return validate_sample_app(sba_c_manifest())


def dataset_seed(name: str, definition: SampleAppDefinition | None = None) -> list[dict[str, Any]]:
    """指定 dataset のシード行を返す(E2E の DB 投入・UI 表示が使う)。無ければ空。"""
    definition = definition or sba_c_definition()
    for ds in definition.datasets:
        if ds.name == name:
            return [dict(row) for row in ds.seed]
    return []


def _summary_record() -> dict[str, Any]:
    m = sba_c_manifest()
    d = sba_c_definition()
    return {
        "id": SBA_C_INSTANCE_ID,
        "plugin_id": m.id,
        "version": m.version,
        "name": m.name,
        "description": m.description,
        "icon": m.icon or "📊",
        "tags": list(m.tags),
        "builtin": True,
        "capabilities": sorted({s.capability for s in d.ai_slots}),
        "screens": [s.key for s in d.screens],
    }


def builtin_sample_apps_c() -> list[dict[str, Any]]:
    """home/実行導線が一覧表示するための SBA-C 要約(1 件)。"""
    return [_summary_record()]


def get_builtin_sample_app_c(app_id: str) -> dict[str, Any] | None:
    """app_id から SBA-C の完全定義(seed 含む)を返す。無ければ None。"""
    if app_id != SBA_C_INSTANCE_ID:
        return None
    m = sba_c_manifest()
    d = sba_c_definition()
    return {
        "id": SBA_C_INSTANCE_ID,
        "plugin_id": m.id,
        "version": m.version,
        "name": m.name,
        "description": m.description,
        "icon": m.icon or "📊",
        "tags": list(m.tags),
        "builtin": True,
        "nl2sql_schema": SBA_C_NL2SQL_SCHEMA,
        "definition": d.model_dump(by_alias=True),
    }
