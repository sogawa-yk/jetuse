"""コア同梱 sample-app SBA-A「問い合わせ/サポート管理」(SBA-02)。

DB には置かずコード同梱(usecases_builtin と同じ方針)。`kind: sample-app` の manifest として
表現し、`sample_app.validate_sample_app` / `validate_composition` を満たす。SBA-A は JetUse の
RAG(File Search)・要約・分類・返信ドラフトを「業務アプリの組込点(aiSlots)」に配置した
リファレンス実装で、以降の SBA-03..05 はこの型に倣う。

データモデル:
  - `faqs`     : FAQ ナレッジベース(question/answer/category)。RAG/返信ドラフトの**知識コーパス**。
  - `inquiries`: 受信した問い合わせ(subject/body/category/status)。分類・要約・返信の対象。

AI 組込スロット(aiSlots)= 実行時バインド機構(`ai_runtime`)が JetUse コア能力へ束縛する点:
  - `faq-answer`      → rag.search : FAQ を根拠にした RAG 回答
  - `auto-classify`   → classify   : 問い合わせの自動分類
  - `summarize-thread`→ summarize  : 問い合わせ内容の要約
  - `reply-draft`     → draft      : FAQ を根拠にした返信ドラフト
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from .manifest import PluginManifest, validate_manifest
from .sample_app import SampleAppDefinition, validate_sample_app


def _thread(*msgs: tuple[str, str, str, str]) -> str:
    """会話メッセージ列を JSON 文字列にする(dataset スキーマに配列型が無いため text へ格納)。

    各メッセージは (role, name, at, text)。role は 'customer' | 'agent'。
    UI(sampleapp.tsx)はこの JSON をパースしてチャット吹き出しで描画する。
    後方互換: 非 JSON の素の文字列(旧 "顧客:/担当:" 改行形式)も UI 側でパースに耐える。
    """
    return json.dumps(
        [{"role": r, "name": n, "at": a, "text": t} for (r, n, a, t) in msgs],
        ensure_ascii=False,
    )

#: コア同梱 SBA-A の固定 ID(プラグイン取込と衝突しない 'builtin-' 規約)。
SBA_A_ID = "jetuse/support-desk"
SBA_A_INSTANCE_ID = "builtin-sba-a"

#: SBA-A の知識コーパスとなる FAQ データセット名(RAG/draft が根拠にする)。
SBA_A_KNOWLEDGE_DATASET = "faqs"

_FAQ_SEED: list[dict[str, Any]] = [
    {
        "question": "パスワードを忘れてログインできません。どうすればいいですか？",
        "answer": "ログイン画面の「パスワードをお忘れですか？」リンクから、登録メールアドレス宛に"
        "再設定用リンクを送信できます。リンクの有効期限は発行から30分です。30分を過ぎた場合は"
        "再度リンクを発行してください。",
        "category": "アカウント",
        "views": 1284,
        "updated_at": "2026-05-30",
    },
    {
        "question": "アカウントがロックされました。解除方法を教えてください。",
        "answer": "パスワードを5回連続で間違えるとアカウントは15分間ロックされます。15分待つと"
        "自動的に解除されます。至急の場合は管理者にロック解除を依頼してください。",
        "category": "アカウント",
        "views": 902,
        "updated_at": "2026-06-02",
    },
    {
        "question": "請求書はどこからダウンロードできますか？",
        "answer": "管理画面の「請求」→「請求履歴」から、月ごとのPDF請求書をダウンロードできます。"
        "請求書は毎月1日に前月分が確定し発行されます。",
        "category": "請求",
        "views": 1530,
        "updated_at": "2026-06-10",
    },
    {
        "question": "支払い方法を変更したい。",
        "answer": "「設定」→「お支払い方法」からクレジットカードまたは銀行振込を"
        "選択・変更できます。変更は次回請求分から反映されます。当月分には適用されません。",
        "category": "請求",
        "views": 671,
        "updated_at": "2026-06-08",
    },
    {
        "question": "無料プランから有料プランへアップグレードするには？",
        "answer": "「プラン」ページで希望のプランを選び「アップグレード」を押してください。"
        "差額は日割りで当月請求に加算されます。ダウングレードは次回更新日からの適用となります。",
        "category": "プラン",
        "views": 845,
        "updated_at": "2026-05-21",
    },
    {
        "question": "データをCSVでエクスポートできますか？",
        "answer": "各一覧画面の右上「エクスポート」ボタンからCSV形式で出力できます。1回の出力上限は"
        "10万行です。それを超える場合は期間を分けて出力してください。",
        "category": "機能",
        "views": 1102,
        "updated_at": "2026-06-15",
    },
    {
        "question": "APIの利用方法とレート制限を知りたい。",
        "answer": "APIキーは「設定」→「API」から発行します。レート制限は標準プランで毎分60、"
        "上位プランで毎分600リクエストです。制限超過時はHTTP 429が返ります。",
        "category": "機能",
        "views": 1378,
        "updated_at": "2026-06-18",
    },
    {
        "question": "サービスが落ちているようですが障害情報はどこで確認できますか？",
        "answer": "稼働状況はステータスページ(status.example.com)で確認できます。"
        "障害発生時はステータスページと登録メールで通知し、復旧見込みも同ページに掲載します。",
        "category": "障害",
        "views": 564,
        "updated_at": "2026-06-19",
    },
    {
        "question": "退会(解約)したい場合の手続きは？",
        "answer": "「設定」→「アカウント」→「解約」から手続きできます。解約後もデータは"
        "30日間保持され、その間は再開可能です。30日経過後にデータは完全に削除されます。",
        "category": "アカウント",
        "views": 489,
        "updated_at": "2026-05-12",
    },
    {
        "question": "対応している言語と文字コードは？",
        "answer": "UI は日本語と英語に対応しています。データの取り込み・出力は UTF-8 を推奨します。"
        "Shift_JIS の取り込みも可能ですが、文字化けを防ぐため UTF-8 をご利用ください。",
        "category": "機能",
        "views": 233,
        "updated_at": "2026-04-28",
    },
]

#: 問い合わせ会話スレッド。各ターンを発言者ロール付き構造化メッセージ(role/name/at/text)の
#: JSON 配列文字列として text フィールドに格納する(`_thread()` 参照。UI はチャット吹き出しで描画)。
_INQUIRY_SEED: list[dict[str, Any]] = [
    {
        "id": "inq-001",
        "subject": "ログインできずアカウントがロックされた",
        "customer": "株式会社山田商事 / 田中 太郎 様",
        "body": "昨日からログインできません。パスワードを数回間違えたところ、ロックされた旨の表示が"
        "出ました。業務に支障が出ているため至急復旧をお願いします。",
        "thread": _thread(
            (
                "customer",
                "田中 太郎 様",
                "2026-06-25T09:12:00",
                "昨日からログインできません。パスワードを数回間違えたところ、ロックされた旨の"
                "表示が出ました。業務に支障が出ているため至急復旧をお願いします。",
            ),
        ),
        "category": "",
        "priority": "",
        "status": "new",
        "received_at": "2026-06-25T09:12:00",
    },
    {
        "id": "inq-002",
        "subject": "今月の請求書が見つからない",
        "customer": "明日工業株式会社 / 佐藤 花子 様",
        "body": "今月分の請求書をダウンロードしたいのですが、どこにあるか分かりません。"
        "経理へ提出するため、早めに入手したいです。",
        "thread": _thread(
            (
                "customer",
                "佐藤 花子 様",
                "2026-06-25T10:40:00",
                "今月分の請求書をダウンロードしたいのですが、どこにあるか分かりません。"
                "経理へ提出するため、早めに入手したいです。",
            ),
        ),
        "category": "",
        "priority": "",
        "status": "new",
        "received_at": "2026-06-25T10:40:00",
    },
    {
        "id": "inq-003",
        "subject": "APIのレート制限を引き上げたい",
        "customer": "テックリード合同会社 / 鈴木 一郎 様",
        "body": "標準プランのAPIレート制限(毎分60)では不足しています。上限を引き上げる方法を"
        "教えてください。",
        "thread": _thread(
            (
                "customer",
                "鈴木 一郎 様",
                "2026-06-24T14:05:00",
                "標準プランのAPIレート制限(毎分60)では不足しています。上限を引き上げる方法を"
                "教えてください。",
            ),
            (
                "agent",
                "サポート 山本",
                "2026-06-24T14:32:00",
                "上位プランへのアップグレードで毎分600リクエストまで引き上げられます。ご検討ください。",
            ),
            (
                "customer",
                "鈴木 一郎 様",
                "2026-06-24T15:10:00",
                "上位プランの料金と切り替えタイミングを教えてください。",
            ),
        ),
        "category": "機能",
        "priority": "中",
        "status": "in_progress",
        "received_at": "2026-06-24T14:05:00",
    },
    {
        "id": "inq-004",
        "subject": "CSVエクスポートが途中で止まる",
        "customer": "グローバル物流株式会社 / 高橋 健 様",
        "body": "約50万行のデータをCSVで出力しようとすると途中でエラーになります。"
        "全件を出力したいのですが対処法はありますか。",
        "thread": _thread(
            (
                "customer",
                "高橋 健 様",
                "2026-06-24T16:30:00",
                "約50万行のデータをCSVで出力しようとすると途中でエラーになります。"
                "全件を出力したいのですが対処法はありますか。",
            ),
        ),
        "category": "機能",
        "priority": "高",
        "status": "in_progress",
        "received_at": "2026-06-24T16:30:00",
    },
    {
        "id": "inq-005",
        "subject": "管理画面に接続できない(障害?)",
        "customer": "株式会社未来システム / 伊藤 美咲 様",
        "body": "先ほどから管理画面が開けません。障害でしょうか。復旧の見込みを知りたいです。",
        "thread": _thread(
            (
                "customer",
                "伊藤 美咲 様",
                "2026-06-23T11:20:00",
                "先ほどから管理画面が開けません。障害でしょうか。復旧の見込みを知りたいです。",
            ),
            (
                "agent",
                "サポート 佐々木",
                "2026-06-23T11:38:00",
                "状況を確認しています。あわせてステータスページもご確認ください。"
                "判明し次第ご連絡します。",
            ),
        ),
        "category": "障害",
        "priority": "高",
        "status": "on_hold",
        "received_at": "2026-06-23T11:20:00",
    },
    {
        "id": "inq-006",
        "subject": "無料プランから有料プランへ変更したい",
        "customer": "さくらデザイン事務所 / 渡辺 彩 様",
        "body": "現在は無料プランですが、有料プランへアップグレードしたいです。"
        "手順と課金のタイミングを教えてください。",
        "thread": _thread(
            (
                "customer",
                "渡辺 彩 様",
                "2026-06-22T13:00:00",
                "現在は無料プランですが、有料プランへアップグレードしたいです。"
                "手順と課金のタイミングを教えてください。",
            ),
            (
                "agent",
                "サポート 山本",
                "2026-06-22T13:25:00",
                "プランページからアップグレードできます。差額は日割りで当月請求に加算されます。",
            ),
            (
                "customer",
                "渡辺 彩 様",
                "2026-06-22T13:40:00",
                "了解しました。ありがとうございます。",
            ),
        ),
        "category": "プラン",
        "priority": "低",
        "status": "resolved",
        "received_at": "2026-06-22T13:00:00",
    },
    {
        "id": "inq-007",
        "subject": "支払い方法をクレジットカードに変更したい",
        "customer": "株式会社あおぞら / 中村 大輔 様",
        "body": "現在は銀行振込ですが、クレジットカード払いに変更したいです。"
        "変更はいつから反映されますか。",
        "thread": _thread(
            (
                "customer",
                "中村 大輔 様",
                "2026-06-25T08:05:00",
                "現在は銀行振込ですが、クレジットカード払いに変更したいです。"
                "変更はいつから反映されますか。",
            ),
        ),
        "category": "",
        "priority": "",
        "status": "new",
        "received_at": "2026-06-25T08:05:00",
    },
]

_DEFINITION: dict[str, Any] = {
    "summary": "サポートデスク(問い合わせ管理)業務アプリ。受信トレイ→詳細対応の業務フローに、"
    "自動トリアージ(分類)・ナレッジ提案(RAG)・返信ドラフト・スレッド要約のAIを埋め込んだ"
    "リファレンス業務アプリ(SBA-A)。",
    "datasets": [
        {
            "name": "faqs",
            "label": "FAQ ナレッジ",
            "fields": [
                {"name": "question", "type": "string", "label": "質問", "required": True},
                {"name": "answer", "type": "text", "label": "回答", "required": True},
                {"name": "category", "type": "string", "label": "カテゴリ"},
                {"name": "views", "type": "number", "label": "参照数"},
                {"name": "updated_at", "type": "date", "label": "更新日"},
            ],
            "seed": _FAQ_SEED,
        },
        {
            "name": "inquiries",
            "label": "問い合わせ",
            "fields": [
                {"name": "id", "type": "string", "label": "ID"},
                {"name": "subject", "type": "string", "label": "件名", "required": True},
                {"name": "customer", "type": "string", "label": "顧客"},
                {"name": "body", "type": "text", "label": "本文", "required": True},
                {"name": "thread", "type": "text", "label": "会話スレッド"},
                {"name": "category", "type": "string", "label": "カテゴリ"},
                {"name": "priority", "type": "string", "label": "優先度"},
                {"name": "status", "type": "string", "label": "ステータス"},
                {"name": "received_at", "type": "datetime", "label": "受信日時"},
            ],
            "seed": _INQUIRY_SEED,
        },
    ],
    "aiSlots": [
        {
            "key": "faq-answer",
            "title": "FAQ-RAG 回答",
            "capability": "rag.search",
            "permissions": ["platform:rag.search"],
        },
        {"key": "auto-classify", "title": "自動分類", "capability": "classify"},
        {"key": "summarize-thread", "title": "問い合わせ要約", "capability": "summarize"},
        {"key": "reply-draft", "title": "返信ドラフト", "capability": "draft"},
    ],
    "screens": [
        {
            "key": "faq",
            "title": "FAQ ナレッジ",
            "type": "list",
            "dataset": "faqs",
            "slots": ["faq-answer"],
        },
        {
            "key": "inbox",
            "title": "問い合わせ一覧",
            "type": "list",
            "dataset": "inquiries",
            "slots": ["auto-classify"],
        },
        {
            "key": "console",
            "title": "対応コンソール",
            "type": "detail",
            "dataset": "inquiries",
            "slots": ["faq-answer", "summarize-thread", "reply-draft"],
        },
    ],
}

_MANIFEST: dict[str, Any] = {
    "schemaVersion": "1",
    "id": SBA_A_ID,
    "version": "1.0.0",
    "kind": "sample-app",
    "name": "問い合わせ/サポート管理",
    "description": "FAQ-RAG 回答・自動分類・要約・返信ドラフトを備えたサポート業務デモ(SBA-A)。",
    "publisher": "jetuse",
    "jetuse": {"minVersion": "0.3.0"},
    "permissions": ["platform:rag.search"],
    "contributes": {"sample-app": _DEFINITION},
    "tags": ["support", "rag", "sample-app"],
    "icon": "💬",
}


@lru_cache(maxsize=1)
def sba_a_manifest() -> PluginManifest:
    """検証済みの SBA-A manifest(kind=sample-app)。"""
    return validate_manifest(_MANIFEST)


@lru_cache(maxsize=1)
def sba_a_definition() -> SampleAppDefinition:
    """検証済みの SBA-A sample-app 定義(screens/datasets/aiSlots)。"""
    return validate_sample_app(sba_a_manifest())


def knowledge_corpus(definition: SampleAppDefinition | None = None) -> list[dict[str, Any]]:
    """RAG/返信ドラフトの根拠となる知識コーパス(FAQ シード行)を返す。"""
    definition = definition or sba_a_definition()
    for ds in definition.datasets:
        if ds.name == SBA_A_KNOWLEDGE_DATASET:
            return [dict(row) for row in ds.seed]
    return []


def builtin_sample_apps() -> list[dict[str, Any]]:
    """home/実行導線が一覧表示するためのコア同梱 sample-app 要約リスト。"""
    m = sba_a_manifest()
    d = sba_a_definition()
    return [
        {
            "id": SBA_A_INSTANCE_ID,
            "plugin_id": m.id,
            "version": m.version,
            "name": m.name,
            "description": m.description,
            "icon": m.icon or "💬",
            "tags": list(m.tags),
            "builtin": True,
            "capabilities": sorted({s.capability for s in d.ai_slots}),
            "screens": [s.key for s in d.screens],
        }
    ]


def get_builtin_sample_app(app_id: str) -> dict[str, Any] | None:
    """app_id から SBA-A の完全定義(seed 含む)を返す。無ければ None。

    公開 API のパス(`/api/sample-apps/{app_id}`)で扱う ID は URL-safe な instance id
    (`builtin-sba-a`)に統一する。plugin_id(`jetuse/support-desk`)は slash を含み path
    parameter にマッチしないため受け付けない(取込済みプラグインの参照は別ルート/レジストリの責務)。
    """
    if app_id != SBA_A_INSTANCE_ID:
        return None
    m = sba_a_manifest()
    d = sba_a_definition()
    return {
        "id": SBA_A_INSTANCE_ID,
        "plugin_id": m.id,
        "version": m.version,
        "name": m.name,
        "description": m.description,
        "icon": m.icon or "💬",
        "tags": list(m.tags),
        "builtin": True,
        "knowledge_dataset": SBA_A_KNOWLEDGE_DATASET,
        "definition": d.model_dump(by_alias=True),
    }
