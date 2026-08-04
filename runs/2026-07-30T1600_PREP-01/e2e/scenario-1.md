# シナリオ1 — `adb`: 出典は**チャンク単位**（シート + セル範囲）

同じ 1 ファイル `jetuse-spike-prep01-サンプル在庫連携API仕様書.xlsx` を取り込んだ結果を検索した（アップロードはアプリ経路
`POST /api/rag/files`。取り込みは `rag.add_file` → `rag_adb.ingest`）。

## 検索（`rag_adb.search` / `current_version='Y'`）

```
ee486231-584d-4d36-8b7b-f7e77fce6c2b-2 | sheet=制約 | cells=C5:E6 | score=0.6126 | レート制限	600 req/min	超過時は HTTP 429 を返す...
ee486231-584d-4d36-8b7b-f7e77fce6c2b-4 | sheet=改訂履歴 | cells=A1:C2 | score=0.6057 | 版	日付	内容...
ee486231-584d-4d36-8b7b-f7e77fce6c2b-3 | sheet=制約 | cells=C40:E40 | score=0.4633 | 同時接続数	50	IP 単位で計数する...
ee486231-584d-4d36-8b7b-f7e77fce6c2b-1 | sheet=制約 | cells=A1 | score=0.4264 | 本仕様書の制約事項...
ee486231-584d-4d36-8b7b-f7e77fce6c2b-0 | sheet=API一覧 | cells=B12:D14 | score=0.3902 | エンドポイント	メソッド	説明...
```

- すべて**同一ファイル**由来: **True**
- (シート, セル範囲) がすべて異なる: **True** → `[('制約', 'C5:E6'), ('改訂履歴', 'A1:C2'), ('制約', 'C40:E40'), ('制約', 'A1'), ('API一覧', 'B12:D14')]`
- セル範囲が A1 形式（列 + 行）である: **True**
- 取り込み時の分類 `kind="spec"` が ADB 側にも入っている: **True**
  （実際: `['spec']`）

## 実 API 経路（`POST /api/chat/stream` / `rag_backend="adb"`）

質問: `レート制限は1分あたり何リクエストですか`

```
600 req/min です。超過した場合は HTTP 429 を返します。
```

引用（`citations[].source`）:

```
[
  {
    "file_id": "ee486231-584d-4d36-8b7b-f7e77fce6c2b",
    "filename": "jetuse-spike-prep01-サンプル在庫連携API仕様書.xlsx",
    "score": 0.6126,
    "source": {
      "chunk_id": "ee486231-584d-4d36-8b7b-f7e77fce6c2b-2",
      "chunk_no": 2,
      "file": "jetuse-spike-prep01-サンプル在庫連携API仕様書.xlsx",
      "version": "2.0",
      "sheet": "制約",
      "cells": "C5:E6",
      "sha256": "2f59f2933968",
      "kind": "spec",
      "current_version": "Y",
      "attributes": {
        "source": "upload",
        "ext": "xlsx"
      }
    },
    "text": "レート制限\t600 req/min\t超過時は HTTP 429 を返す\nデータ保持期間\t13か月\t明細データが対象"
  },
  {
    "file_id": "ee486231-584d-4d36-8b7b-f7e77fce6c2b",
    "filename": "jetuse-spike-prep01-サンプル在庫連携API仕様書.xlsx",
    "score": 0.6057,
    "source": {
      "chunk_id": "ee486231-584d-4d36-8b7b-f7e77fce6c2b-4",
      "chunk_no": 4,
      "file": "jetuse-spike-prep01-サンプル在庫連携API仕様書.xlsx",
      "version": "2.0",
      "sheet": "改訂履歴",
      "cells": "A1:C2",
      "sha256": "2f59f2933968",
      "kind": "spec",
      "current_version": "Y",
      "attributes": {
        "source": "upload",
        "ext": "xlsx"
      }
    },
    "text": "版\t日付\t内容\n2.0\t2026-07-30\tレート制限を 600 req/min に改訂"
  },
  {
    "file_id": "ee486231-584d-4d36-8b7b-f7e77fce6c2b",
    "filename": "jetuse-spike-prep01-サンプル在庫連携API仕様書.xlsx",
    "score": 0.4633,
    "source": {
      "chunk_id": "ee486231-584d-4d36-8b7b-f7e77fce6c2b-3",
      "chunk_no": 3,
      "file": "jetuse-spike-prep01-サンプル在庫連携API仕様書.xlsx",
      "version": "2.0",
      "sheet": "制約",
      "cells": "C40:E40",
      "sha256": "2f59f2933968",
      "kind": "spec",
      "current_version": "Y",
      "attributes": {
        "source": "upload",
        "ext": "xlsx"
      }
    },
    "text": "同時接続数\t50\tIP 単位で計数する"
  },
```

- 引用の (シート, セル範囲) がチャンクごとに異なる: **True**

判定: **PASS**

> これは `adb` バックエンドだけの粒度である。マネージド Vector Store は属性が
> **ファイル単位**（SPIKE-M1 ①-a）で、同じファイルの全チャンクが同じ出典しか返せない
> （シナリオ2 で実測）。
