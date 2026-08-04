# シナリオ1: 版違い2件の取り込みと版フィルタの対照（実 API）

## 取り込み（POST /api/rag/files + attributes）

| ファイル | version | current_version | cells |
|---|---|---|---|
| `inventory-api-spec-v1.md` | 1.0 | N | B12:F12 |
| `inventory-api-spec-v2.md` | 2.0 | Y | B18:F18 |

取り込み状態: {"inventory-api-spec-v2.md": "completed", "inventory-api-spec-v1.md": "completed"}

## Vector Store に保持された attributes（files.retrieve の実値）

```json
{
  "inventory-api-spec-v1.md": {
    "file": "架空サンプル_在庫連携API仕様書.xlsx",
    "sha256": "33def37ca90f9c657bb0fa97ce4f5cec5c6ebe9947d3aa037e85de86e6aa00d9",
    "version": "1.0",
    "sheet": "API一覧",
    "cells": "B12:F12",
    "kind": "spec",
    "current_version": "N"
  },
  "inventory-api-spec-v2.md": {
    "file": "架空サンプル_在庫連携API仕様書.xlsx",
    "sha256": "2f56fb6b9a24732c13c8937f63006c63881da6fbbcfa3e9d47b4811a28e3a3f8",
    "version": "2.0",
    "sheet": "API一覧",
    "cells": "B18:F18",
    "kind": "spec",
    "current_version": "Y"
  }
}
```

## 対照（同一クエリ / フィルタ有無だけが違う）

クエリ: `在庫照会APIは一度に最大何件まで返しますか。件数と根拠の版を答えてください。`

| 条件 | 引用ファイル | 引用の version |
|---|---|---|
| フィルタ無し | ['inventory-api-spec-v1.md', 'inventory-api-spec-v2.md'] | ['1.0', '2.0'] |
| `current_version=Y` | ['inventory-api-spec-v2.md'] | ['2.0'] |

### フィルタ無しの回答

```
在庫照会 API は **一度に最大 200 件** まで返却します。  

**根拠**  
- 「架空サンプル 在庫連携API仕様書 v2.0（最新版）」の記載（inventory‑api‑spec‑v2.md）に「在庫照会API GET /v1/inventory は一度に最大200件まで返却する。」とあります。  

（※旧版 v1.0 では 100 件が上限でしたが、最新版 v2.0 で 200 件に改定されています。）
```

### 版フィルタ有りの回答

```
在庫照会API（GET /v1/inventory）は、一度の呼び出しで **最大200件** まで返却します。  
（※以前の v1.0 では100件が上限でしたが、最新版（v2.0）ではこの上限が撤廃され、200件に拡大されています）  

**根拠**  
- 文書名: **inventory-api-spec-v2.md**（架空サンプル 在庫連携API仕様書 v2.0）  
- 該当箇所: 「在庫照会API GET /v1/inventory は一度に最大200件まで返却する。 v1.0 の100件という上限は本版で廃止された。」
```

### 引用（フィルタ有り・構造化された出典）

```json
[
  {
    "file_id": "file-<REDACTED>",
    "filename": "inventory-api-spec-v2.md",
    "score": 0.943,
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
