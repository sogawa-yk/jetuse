# シナリオ2 — 画像（PNG）が検索でヒットする

架空の受入検査記録を 1 枚の PNG（`jetuse-spike-prep03-受入検査記録.png`）にしたものを、
同じアプリ経路で取り込んだ。画像は**1 ページ扱い**。

- file_id: `311c859d-6217-4449-b76b-996a03557921` / 取り込み状態: **completed**
- バックエンド別の取り込み状況: `{'vector_store': 'indexed', 'select_ai': 'pending', 'opensearch': 'disabled', 'adb': 'indexed'}`
- このアップロードで走った OCR:

```
[
  {
    "engine": "ocr",
    "bytes": 62516,
    "pages": 1,
    "seconds": 2.0,
    "mean_confidence": 0.9741
  }
]
```

## 検索（`rag_adb.search`）

質問: `受入検査のロット番号と判定結果は何ですか`

```
311c859d-6217-4449-b76b-996a03557921-0 | sheet=p.1 | cells=L1:L11 | score=0.6294 | 架空製作所...
```

- 本文の語 `LOT-2026-0518` を含むチャンクがヒットする（空白を無視して比較）:
  **True**

## OCR した本文（そのまま）

```
架空製作所
受入検査記録
ロット番号:
LOT - 2026 - 0518
検査項目:
外径寸法 42. 0 mm ± 0. 05
判定:
合格(測定値 41. 98 mm)
検査員:
品質保証部
佐藤
```

## 実 API 経路（`POST /api/chat/stream` / `rag_backend="adb"`）

```
ロット番号は「LOT - 2026 - 0518」で、判定結果は「合格」（測定値 41.98 mm）です。
```

引用:

```
[
  {
    "file_id": "311c859d-6217-4449-b76b-996a03557921",
    "filename": "jetuse-spike-prep03-受入検査記録.png",
    "score": 0.6294,
    "source": {
      "chunk_id": "311c859d-6217-4449-b76b-996a03557921-0",
      "chunk_no": 0,
      "file": "jetuse-spike-prep03-受入検査記録.png",
      "version": "11.0",
      "sheet": "p.1",
      "cells": "L1:L11",
      "sha256": "6ee570deab67",
      "kind": "doc",
      "current_version": "Y",
      "attributes": {
        "source": "upload",
        "ext": "png"
      }
    },
    "text": "架空製作所\n受入検査記録\nロット番号:\nLOT - 2026 - 0518\n検査項目:\n外径寸法 42. 0 mm ± 0. 05\n判定:\n合格(測定値 41. 98 mm)\n検査員:\n品質保証部\n佐藤"
  },
  {
    "file_id": "7c3adb2b-4c4a-4adf-abba-a0be6d75d35b",
    "filename": "jetuse-spike-prep03-設備点検報告書スキャン.pdf",
    "score": 0.4608,
    "source": {
      "chunk_id": "7c3adb2b-4c4a-4adf-abba-a0be6d75d35b-1",
      "chunk_no": 1,
      "file": "jetuse-spike-prep03-設備点検報告書スキャン.pdf",
      "version": "11.0",
      "sheet": "p.2",
      "cells": "L1:L9",
      "sha256": "8b1e97193f86",
      "kind": "doc",
      "current_version": "Y",
      "attributes": {
        "source": "upload",
        "ext": "pdf"
      }
    },
    "text": "是正処置および交換部品\n処置区分:\n予防保全(計画停止中に実施)\n交換部品コード:\nBRG - 778 1\n交換期限:\n2026年6月30 日まで\n備考:\n次回点検で振動値の再測定を行う。"
  },
  {
    "file_id": "7c3adb2b-4c4a-4adf-abba-a0be6d75d35b",
    "filename": "jetuse-spike-prep03-設備点検報告書スキャン.pdf",
    "score": 0.4327,
    "source": {
      "chunk_id": "7c3adb2b-4c4a-4adf-abba-a0be6d75d3
```

- 画像由来の引用のページが `p.1`: **True** → `['p.1']`
- 回答に `LOT-2026-0518`（空白を無視）と判定 `合格` が出る: **True**

判定: **PASS**
