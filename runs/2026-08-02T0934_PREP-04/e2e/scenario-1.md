# シナリオ1 — 1 セル 13,000 文字級の xlsx が取り込める

**架空**のブック `jetuse-spike-prep04-サンプル在庫連携API仕様書-リクエスト例.xlsx`（顧客の実ファイルは使っていない）。実データと同じ形:
`サンプル` シートの **A53 に 13,081 文字**があり、その行の非空セルは 1 個。
**行境界でもセル境界でも割れない**ので、以前はここで `422 limit=chunk_chars` になり
**ファイル全体が入らなかった**。

- 取り込み: `POST /api/rag/files` → file_id `fa05d4a7-f41c-41e0-8472-3504d22475d6` /
  バックエンド `{'vector_store': 'indexed', 'select_ai': 'pending', 'opensearch': 'disabled', 'adb': 'indexed'}` / 状態 `completed`

## (a) 取り込み経路が作るチャンク（`POST /api/extract`・保存しない口）

- HTTP ステータス: **200**（以前はここが 422 だった）
- チャンク総数: **8** / うち `A53` 由来の断片: **7**
- 断片の `part`: `['1/7', '2/7', '3/7', '4/7', '5/7', '6/7', '7/7']`（**黙って分割していない**ことが応答から分かる）
- 断片をつなぐと元のセルの値に**完全に一致**する（1 文字も落ちていない）: **True**

```
サンプル | A1:A2 | part=None | 45 文字 | 在庫連携API リクエスト例（サンプル）...
サンプル | A53 | part=1/7 | 1976 文字 | ### 1. 出荷指示 API...
サンプル | A53 | part=2/7 | 1995 文字 |   "reason_code": "RE-007",...
サンプル | A53 | part=3/7 | 1990 文字 | ### 14. 棚卸差異 API...
サンプル | A53 | part=4/7 | 1988 文字 |   "warehouse": "WA-020",...
サンプル | A53 | part=5/7 | 1945 文字 | 注意: 出荷済みの指示は取り消せない。訂正は返品伝票で行う。...
サンプル | A53 | part=6/7 | 1976 文字 |     {"sku": "SKU-0032", "qty": 33, "lot"...
サンプル | A53 | part=7/7 | 1211 文字 | 棚卸の差異を登録する。呼び出しは `POST /v1/stocktakes` で...
```

## (b) 検索（`rag_adb.search` / `current_version='Y'`）

質問: `冪等キーは何時間有効ですか`

```
fa05d4a7-f41c-41e0-8472-3504d22475d6-4 | sheet=サンプル | cells=A53 | score=0.5615 |   "warehouse": "WA-020",...
fa05d4a7-f41c-41e0-8472-3504d22475d6-5 | sheet=サンプル | cells=A53 | score=0.41 | 注意: 出荷済みの指示は取り消せない。訂正は返品伝票で行う。...
fa05d4a7-f41c-41e0-8472-3504d22475d6-7 | sheet=サンプル | cells=A53 | score=0.3932 | 棚卸の差異を登録する。呼び出しは `POST /v1/stocktakes` で...
fa05d4a7-f41c-41e0-8472-3504d22475d6-2 | sheet=サンプル | cells=A53 | score=0.3903 |   "reason_code": "RE-007",...
fa05d4a7-f41c-41e0-8472-3504d22475d6-3 | sheet=サンプル | cells=A53 | score=0.3882 | ### 14. 棚卸差異 API...
```

- セルの**中ほど**（13,081 文字中およそ 6,745 文字目）に書いた
  `冪等キー(idempotency_key)は発行から24時間有効` を含む断片がヒットする: **True**
  → 先頭 2,000 文字だけが検索対象になっているのではない
- ヒットの出典シートがすべて `サンプル`: **True**

## (c) 実 API 経路（`POST /api/chat/stream` / `rag_backend="adb"`）

```
24時間有効です。
```

引用（`citations[].source`）:

```
[
  {
    "file_id": "fa05d4a7-f41c-41e0-8472-3504d22475d6",
    "filename": "jetuse-spike-prep04-サンプル在庫連携API仕様書-リクエスト例.xlsx",
    "score": 0.5615,
    "source": {
      "chunk_id": "fa05d4a7-f41c-41e0-8472-3504d22475d6-4",
      "chunk_no": 4,
      "file": "jetuse-spike-prep04-サンプル在庫連携API仕様書-リクエスト例.xlsx",
      "version": "1.0",
      "sheet": "サンプル",
      "cells": "A53",
      "sha256": "8b4c3dd8446c",
      "kind": "spec",
      "current_version": "Y",
      "attributes": {
        "source": "upload",
        "ext": "xlsx"
      }
    },
    "text": "  \"warehouse\": \"WA-020\",\n  \"items\": [\n    {\"sku\": \"SKU-0020\", \"qty\": 21, \"lot\": \"L-2026-0020\"}\n  ]\n}\nレスポンス例: {\"status\": \"accepted\", \"request_id\": \"REQ-0020\"}\n### 21. 出荷指示 API\n出荷指示を登録し追跡番号を採番する。呼び出しは `POST /v1/shipments` で、`carrier` は必須項目である。\n注意: 出荷済みの指示は取り消せない。訂正は返品伝票で行う。\nリクエスト例:\n{\n  \"request_id\": \"REQ-0021\",\n  \"carrier\": \"CA-021\",\n  \"items\": [\n    {\"sku\": \"SKU-0021\", \"qty\": 22, \"lot\": \"L-2026-002"
  },
  {
    "file_id": "fa05d4a7-f41c-41e0-8472-3504d22475d6",
    "filename": "jetuse-spike-prep04-サンプル在庫連携API仕様書-リクエスト例.xlsx",
    "score": 0.41,
    "source": {
      "chunk_id": "fa05d4a7-f41c-41e0-8472-3504d22475d6-5",
      "chunk_no": 5,
      "file": "jetuse-spike-prep04-サンプル在庫連携API仕様書-リクエスト例.xlsx",
      "version": "1.0",
      "sheet": "サンプル",
      "cells": "A53",
      "sha256": "8b4c3dd8446c",
      "kind": "spec",
      "current_version": "Y",
      "attributes": {
        "source": "upload",
        "ext": "xlsx"
      }
    },
    "text": "注意: 出荷済みの指示は取り消せ
```

- 引用の (シート, セル範囲) が**元のセル** `サンプル A53`: **True**
  → 実際: `[('サンプル', 'A53'), ('サンプル', 'A53'), ('サンプル', 'A53'), ('サンプル', 'A53'), ('サンプル', 'A53')]`

判定: **PASS**
