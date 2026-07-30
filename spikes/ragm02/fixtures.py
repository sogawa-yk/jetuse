"""スケール検証用の**架空**チャンク生成（顧客データは一切使わない）。

意味的に固まり（クラスタ）のある分布にする。乱数ベクタを並べると近似検索の再現率が
実運用と乖離するため、実際の埋め込み API に通す文章そのものを話題別に作る。
"""

import hashlib

TOPICS = [
    ("在庫照会API", "GET /v1/inventory で在庫数と引当可能数を返す"),
    ("認証トークン", "アクセストークンの有効期限は3600秒で更新は再発行のみ"),
    ("レート制限", "1分あたりの上限リクエスト数を超えると429を返す"),
    ("データ保持期間", "明細は13か月保持しその後は月次集計のみ残す"),
    ("障害時の再送", "5xxのときは指数バックオフで最大5回まで再送する"),
    ("締め処理", "日次締めは翌営業日の未明に確定しその後の更新を拒否する"),
    ("マスタ同期", "商品マスタは差分連携で洗い替えは行わない"),
    ("権限管理", "参照権限と更新権限を役割単位で分離して付与する"),
    ("監査ログ", "更新系の操作は利用者と旧値新値を記録する"),
    ("バックアップ", "日次フルと1時間ごとの差分を取得する"),
    ("文字コード", "連携ファイルはUTF-8のBOM無しとする"),
    ("端数処理", "金額の端数は四捨五入ではなく切り捨てとする"),
    ("配送区分", "宅配便とチャーター便で運賃計算式が異なる"),
    ("返品処理", "返品は入荷検品を経てから在庫へ戻す"),
    ("ロット管理", "同一商品でもロットが異なれば別在庫として扱う"),
    ("賞味期限", "残存期間が3分の1を切った在庫は出荷対象から外す"),
    ("棚卸", "循環棚卸は区画単位で毎月実施する"),
    ("発注点", "発注点を下回った時点で補充発注を自動起票する"),
    ("入荷予定", "入荷予定日は仕入先確定分のみ確定として扱う"),
    ("出荷指示", "出荷指示は波取り単位でまとめて発行する"),
    ("送り状", "送り状番号は出荷確定時に採番して以後変更しない"),
    ("課金", "従量課金は月末締めで翌月10日に請求する"),
    ("SLA", "可用性は月間99.5パーセント以上を目標とする"),
    ("用語定義", "引当済在庫とは出荷指示済で未出荷の数量をいう"),
    ("検品", "検品は員数と外装破損の両方を確認する"),
]
SHEETS = ["API一覧", "制約", "用語", "改訂履歴"]
KINDS = ["spec", "constraint", "glossary"]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def gen_chunks(n: int, *, chunks_per_doc: int = 200, stale_every: int = 3) -> list[dict]:
    """架空チャンクを n 件生成する。

    - `stale_every` 件に 1 件を旧版（`current_version='N'`）にする＝版フィルタの対照が作れる。
    - 話題は 25 種を巡回させ、数値だけ振って near-duplicate を大量に作る
      （近似検索が「似た大量の候補」から正解を引けるかを見たいので、これが要る）。
    """
    out: list[dict] = []
    for i in range(n):
        doc_no = i // chunks_per_doc
        idx = i % chunks_per_doc
        topic, detail = TOPICS[i % len(TOPICS)]
        stale = (i % stale_every) == 0
        doc_file = f"サンプル業務仕様書_{doc_no:04d}.xlsx"
        row = 3 + idx
        text = (
            f"{doc_file} 第{idx // 10 + 1}章 {topic}。{detail}。"
            f"設定値は{100 + (i % 900)}であり、対象は区分{i % 7}の取引に限る。"
        )
        out.append({
            "chunk_id": f"s{i:07d}",
            "file_id": f"doc{doc_no:04d}",
            "chunk_no": idx,
            "doc_file": doc_file,
            "doc_version": "1.0" if stale else "2.0",
            "sheet_name": SHEETS[i % len(SHEETS)],
            "cells": f"B{row}:F{row}",
            "sha256": _sha(text),
            "kind": KINDS[i % len(KINDS)],
            "current_version": "N" if stale else "Y",
            "topic": topic,
            "text": text,
        })
    return out


def queries(n: int = 20) -> list[dict]:
    """検索クエリ（チャンク本文の言い換え）。正解が存在する問い方にする。"""
    out = []
    for i in range(n):
        topic, detail = TOPICS[i % len(TOPICS)]
        out.append({
            "topic": topic,
            "q": f"{topic}について、{detail}という決まりは今の版でも有効ですか。設定値も知りたい。",
        })
    return out
