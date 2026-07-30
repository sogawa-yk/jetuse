# シナリオ2: 回答の citations にセル範囲まで載る（実レスポンス）

`GET /api/chat/stream` の `citations` イベント（版フィルタ有り）。
`source` は本文に埋め込んだ文字列ではなく、取り込み時 attributes に由来する構造化値。

```json
[
  {
    "file_id": "file-<REDACTED>",
    "filename": "inventory-api-spec-v2.md",
    "score": 0.941,
    "source": {
      "file": "架空サンプル_在庫連携API仕様書.xlsx",
      "sha256": "2f56fb6b9a24732c13c8937f63006c63881da6fbbcfa3e9d47b4811a28e3a3f8",
      "version": "2.0",
      "sheet": "API一覧",
      "cells": "B18:F18",
      "kind": "spec",
      "current_version": "Y"
    },
    "text": "inventory-api-spec-v2.md\n\n架空サンプル 在庫連携API仕様書 v2.0（最新版）\n在庫照会API GET /v1/inventory は一度に最大200件まで返却する。\nv1.0 の100件という上限は本版で廃止された。",
    "chunk_id": "0_008ff292-2aca-4d9e-be03-b5e14cc1d714"
  }
]
```

## 後方互換の確認

- 既存フロントが読む `file_id` / `filename` / `score`: {"file_id": "file-<REDACTED>", "filename": "inventory-api-spec-v2.md", "score": 0.941}
- 追加フィールド: `source`（cells, current_version, file, kind, sha256, sheet, version）/ `text` / `chunk_id`

## セル範囲

- sheet = `API一覧` / cells = `B18:F18` / version = `2.0`
