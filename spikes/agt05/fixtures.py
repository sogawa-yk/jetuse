"""AGT-05 の E2E で使う**架空**の受付フロー（顧客データ・案件名は持ち込まない）。

AGT-04 の題材（`spikes/agt04/fixtures.py`）を、実案件の形にもう一歩寄せたもの:
- 業務 API を**順に 8 本**呼ぶ手続き（各 API は入れ子の引数を取る — ADR-0024）
- 仕様書を**3 冊に割る**（手順・コード表・制約集）。1 本の API を呼ぶ前に、順序と
  コード値と制約を**別々の本から**引き直すことになる ＝ 検索が API の本数ではなく
  「引くべき事実の数」に比例して伸びる、という実案件の形（ADR-0026 の背景）

相手は `.env` の `AGT05_ECHO_URL`（受け取った JSON をそのまま返す公開 https エコー）。
**秘密は送らない**（このシナリオに認証は要らない）。
"""

import io
import pathlib
import sys

from openpyxl import Workbook

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "ops"))
import _adb as adb  # noqa: E402  環境変数 → .env の順で読む(ops と同じ流儀)

# OCI 側・検証用の資源には接頭辞を付ける（CLAUDE.md の運用規約）
PREFIX = "jetuse-spike-agt05"

# 相手の宛先はコードに焼かない（環境依存値は .env。雛形は .env.example）。
# 未設定なら e2e は実行を断る = 承認していない宛先へ送らない
ECHO_URL = adb.env("AGT05_ECHO_URL").strip()

ORDER_ID = "ORD-9902"          # 架空の受付番号
PLAN_CODE = "PL-GOLD-24"
BILLING_CODE = "PAY-BANK-02"
EQUIPMENT_CODE = "EQ-RTR-500"
SLOT_CODE = "SLOT-AM-1"
NOTIFY_CODE = "NTF-MAIL-01"
SUPPORT_CODE = "SUP-STD-03"

# 仕様書 3 冊。**事実を本ごとに散らす**（順序は手順書・コード値はコード表・
# 引数の制約は制約集）ので、1 本の API を呼ぶのに複数回の検索が要る。
BOOKS: dict[str, dict[str, list[list[str]]]] = {
    "手順書": {
        "手順": [
            ["受付フローの呼び出し順序", "", ""],
            ["順序", "API（ツール名）", "説明"],
            ["1", "create_reception", "受付を新規作成する。最初に必ずこれを呼ぶ"],
            ["2", "set_contractor", "契約者情報（氏名・電話・住所）を登録する"],
            ["3", "set_service_plan", "料金プランを設定する"],
            ["4", "set_billing", "支払方法を設定する"],
            ["5", "add_equipment", "貸与機器を登録する"],
            ["6", "set_installation_slot", "工事枠を予約する"],
            ["7", "set_notification", "通知設定を登録する"],
            ["8", "confirm_reception", "受付を確定する。最後に必ずこれを呼ぶ"],
            [],
            ["順序の制約", "", ""],
            ["confirm_reception は 1〜7 がすべて成功したあとにだけ呼べる", "", ""],
            ["set_billing の前に set_service_plan を済ませること", "", ""],
            ["set_installation_slot の前に add_equipment を済ませること", "", ""],
        ],
    },
    "コード表": {
        "コード": [
            ["料金プランコード", "", ""],
            ["コード", "名称", "月額"],
            [PLAN_CODE, "ゴールド24", "7800"],
            ["PL-SILVER-12", "シルバー12", "4800"],
            [],
            ["支払方法コード", "", ""],
            ["コード", "方法", "備考"],
            [BILLING_CODE, "銀行口座振替", "口座情報は account に入れる"],
            ["PAY-CARD-01", "クレジットカード", "本フローでは使用しない"],
            [],
            ["貸与機器コード", "", ""],
            ["コード", "機器", "台数上限"],
            [EQUIPMENT_CODE, "宅内ルータ 500 型", "2"],
            [],
            ["工事枠コード", "", ""],
            ["コード", "時間帯", "備考"],
            [SLOT_CODE, "午前（9:00-12:00）", "本フローの既定枠"],
            [],
            ["通知手段コード", "", ""],
            ["コード", "手段", "備考"],
            [NOTIFY_CODE, "メール", "本フローの既定"],
            [],
            ["サポートプランコード", "", ""],
            ["コード", "内容", "備考"],
            [SUPPORT_CODE, "標準サポート", "通知設定と一緒に登録する"],
        ],
    },
    "制約集": {
        "制約": [
            ["契約者情報の制約", "", ""],
            ["電話番号はハイフンなしの数字のみで送ること", "", ""],
            ["住所は郵便番号・都道府県・それ以降の 3 つに分けて送ること", "", ""],
            [],
            ["料金プランの制約", "", ""],
            ["プランコードは「コード」シートの値をそのまま使うこと", "", ""],
            ["オプションは配列で渡す。無い場合は空配列にすること", "", ""],
            [],
            ["支払方法の制約", "", ""],
            ["銀行口座振替では account に bank / branch / number を入れること", "", ""],
            [],
            ["工事枠の制約", "", ""],
            ["工事希望日は YYYY-MM-DD 形式で送ること", "", ""],
            ["時間帯は工事枠コードで指定し、文字列で書かないこと", "", ""],
            [],
            ["通知設定の制約", "", ""],
            ["通知手段コードとサポートプランコードの両方を入れること", "", ""],
            ["宛先メールアドレスは contact に入れること", "", ""],
            [],
            ["確定の制約", "", ""],
            ["confirm_reception には受付番号と確定者を必ず入れること", "", ""],
        ],
    },
}

SPEC_NAMES = {title: f"{PREFIX}-{title}.xlsx" for title in BOOKS}

# 検索だけで完結する問い（シナリオ2 で検索の上限に当てるために使う）
SEARCH_ONLY_QUESTION = (
    "アップロード済みの仕様書から、次をすべて調べて列挙してください。"
    "(1) API の呼び出し順序 (2) 料金プランコード (3) 支払方法コード (4) 貸与機器コード "
    "(5) 工事枠コード (6) 通知手段コード (7) サポートプランコード "
    "(8) 契約者情報の制約 (9) 工事枠の制約 (10) 通知設定の制約。"
    "**1 項目ずつ文書検索して確かめ**、推測せず仕様書に書いてある値だけを答えること"
)


def spec_workbook(title: str) -> bytes:
    """仕様書（架空）。シートごとに空行で区切った塊を置く = チャンクの出典が分かれる。"""
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in BOOKS[title].items():
        ws = wb.create_sheet(title=name)
        for row in rows:
            ws.append(row or [None])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "url": ECHO_URL,
        "method": "POST",
        "parameters": {"type": "object", "properties": properties, "required": required},
    }


_ORDER = {"type": "string", "description": "受付番号（例 ORD-9902）"}


def http_tools() -> list[dict]:
    """順に呼ぶ 8 本。入れ子オブジェクト・配列を含む（ADR-0024 の受理範囲）。"""
    return [
        _tool("create_reception", "受付を新規作成する。受付フローで最初に呼ぶ",
              {"order_id": _ORDER,
               "channel": {"type": "string", "description": "受付チャネル（web/店頭）"}},
              ["order_id", "channel"]),
        _tool("set_contractor", "受付に契約者情報（氏名・電話・住所）を登録する",
              {"order_id": _ORDER,
               "contractor": {
                   "type": "object", "description": "契約者情報",
                   "properties": {
                       "name": {"type": "string", "description": "氏名"},
                       "phone": {"type": "string", "description": "電話番号"},
                       "address": {
                           "type": "object", "description": "住所",
                           "properties": {
                               "zip": {"type": "string", "description": "郵便番号"},
                               "prefecture": {"type": "string", "description": "都道府県"},
                               "rest": {"type": "string", "description": "市区町村以降"},
                           },
                           "required": ["zip", "prefecture", "rest"],
                       },
                   },
                   "required": ["name", "phone", "address"],
               }},
              ["order_id", "contractor"]),
        _tool("set_service_plan", "受付に料金プランを設定する",
              {"order_id": _ORDER,
               "plan": {
                   "type": "object", "description": "料金プラン",
                   "properties": {
                       "code": {"type": "string", "description": "プランコード"},
                       "options": {
                           "type": "array", "description": "オプション（無ければ空配列）",
                           "items": {
                               "type": "object",
                               "properties": {
                                   "code": {"type": "string", "description": "オプションコード"}
                               },
                               "required": ["code"],
                           },
                       },
                   },
                   "required": ["code", "options"],
               }},
              ["order_id", "plan"]),
        _tool("set_billing", "受付に支払方法を設定する",
              {"order_id": _ORDER,
               "billing": {
                   "type": "object", "description": "支払方法",
                   "properties": {
                       "method_code": {"type": "string", "description": "支払方法コード"},
                       "account": {
                           "type": "object", "description": "口座情報（口座振替のとき）",
                           "properties": {
                               "bank": {"type": "string", "description": "銀行名"},
                               "branch": {"type": "string", "description": "支店名"},
                               "number": {"type": "string", "description": "口座番号"},
                           },
                           "required": ["bank", "branch", "number"],
                       },
                   },
                   "required": ["method_code", "account"],
               }},
              ["order_id", "billing"]),
        _tool("add_equipment", "受付に貸与機器を登録する",
              {"order_id": _ORDER,
               "items": {
                   "type": "array", "description": "貸与機器の明細",
                   "items": {
                       "type": "object",
                       "properties": {
                           "code": {"type": "string", "description": "機器コード"},
                           "qty": {"type": "integer", "description": "台数"},
                       },
                       "required": ["code", "qty"],
                   },
               }},
              ["order_id", "items"]),
        _tool("set_installation_slot", "受付に工事枠（希望日と時間帯）を予約する",
              {"order_id": _ORDER,
               "slot": {
                   "type": "object", "description": "工事枠",
                   "properties": {
                       "date": {"type": "string", "description": "工事希望日"},
                       "slot_code": {"type": "string", "description": "工事枠コード"},
                   },
                   "required": ["date", "slot_code"],
               }},
              ["order_id", "slot"]),
        _tool("set_notification", "受付に通知設定（通知手段とサポートプラン）を登録する",
              {"order_id": _ORDER,
               "notification": {
                   "type": "object", "description": "通知設定",
                   "properties": {
                       "method_code": {"type": "string", "description": "通知手段コード"},
                       "support_code": {"type": "string", "description": "サポートプランコード"},
                       "contact": {"type": "string", "description": "宛先"},
                   },
                   "required": ["method_code", "support_code", "contact"],
               }},
              ["order_id", "notification"]),
        _tool("confirm_reception", "受付を確定する。受付フローで最後に呼ぶ",
              {"order_id": _ORDER,
               "confirmed_by": {"type": "string", "description": "確定者"}},
              ["order_id", "confirmed_by"]),
    ]


TOOL_NAMES = [t["name"] for t in http_tools()]

# 手続きの依頼（**手順そのものは書かない**。順序・コード・制約は仕様書から引かせる）
PROCEDURE_REQUEST = f"""受付番号 {ORDER_ID} の新規受付を、登録済みの業務 API で
最後まで登録してください。

呼ぶ順序・使うコード値・引数の制約は、アップロード済みの仕様書（手順書・コード表・制約集）に
分かれて書いてあります。**各 API を呼ぶ前に文書検索で確かめながら**、確定まで進めてください。

受付の内容（これ以外は仕様書に従うこと）:
- 受付チャネル: web
- 契約者: 架空 太郎 / 電話 09012345678 / 〒100-0001 東京都 千代田区千代田1-1
- 支払口座: サンプル銀行 本店 1234567
- 貸与機器: 1 台
- 工事希望日: 2026-09-01
- 通知の宛先: sample@example.com
- 確定者: 検証担当
"""
