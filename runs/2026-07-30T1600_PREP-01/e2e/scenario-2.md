# シナリオ2 — `vector_store`: 属性は**ファイル単位**（能力差の証跡）

シナリオ1 と**同じファイル**をマネージド Vector Store 側で見た。ADR-0020 の決定
（2 バックエンドの能力差）の裏付けになる部分なので、3 つの角度から記録する。

## (a) マネージド側は xlsx をそのまま受け付けるか

`files.create(...xlsx...)` → `vector_stores.files.create` を素の xlsx で試した結果:

```
BadRequestError: Error code: 400 - {'error': {'code': 'unsupported_file', 'message': "Unsupported file type 'xlsx' found for file-kix-… ", 'param': None, 'type': 'unsupported_file', 'valid': True}}
```

→ だから取り込み経路では**抽出したテキストを `<原名>.xlsx.txt` として渡す**
（SPIKE-03 で docx が `Unsupported file type` だったのと同じ扱い）。
素の xlsx が `unsupported_file` で断られた: **True**（この行が判定条件）

## (b) 取り込んだファイルの属性（`vector_stores.files.retrieve`）

```
file-kix-bed… | status=completed | attributes={"file": "jetuse-spike-prep01-サンプル在庫連携API仕様書.xlsx", "sha256": "3da7abfa7455241e27bab43b73d27e039a9feaf7ddd39fe1a13250765847848a", "version": "1.0", "kind": "spec", "sheet": "(ブック全体: 3 シート)", "cells": "(ブック全体)"}
file-kix-045… | status=completed | attributes={"file": "jetuse-spike-prep01-サンプル在庫連携API仕様書.xlsx", "sha256": "2f59f29339682d186cd3075f53db9de4bf03603eadaf62bf0a2e52a78e7f07dc", "version": "2.0", "kind": "spec", "sheet": "(ブック全体: 3 シート)", "cells": "(ブック全体)"}
```

- `sheet` / `cells` が**ブック全体**を表す値になっている: **True**
- 取り込み状態がすべて `completed`: **True**（実際: `['completed', 'completed']`）
- 1 ファイルにつき属性は **1 種類**。チャンクが何個できても増えない（SPIKE-M1 ①-a）

## (c) 実 API の RAG 応答（`POST /api/chat/stream` / 既定の `vector_store`）

質問: `レート制限は1分あたり何リクエストですか`

```
アップロードされた **「jetuse‑spike‑prep01‑サンプル在庫連携API仕様書.xlsx」** の最新版（バージョン 2.0）では、レート制限は **600 req／min** と規定されています。  

> 「レート制限 600 req/min 超過時は HTTP 429 を返す」​（同ファイル、制約 C5:E6）  

したがって、1分あたりの許容リクエスト数は **600 件** です。
```

引用:

```
[
  {
    "file_id": "file-kix-…",
    "filename": "jetuse-spike-prep01-サンプル在庫連携API仕様書.xlsx",
    "score": 0.75,
    "source": {
      "file": "jetuse-spike-prep01-サンプル在庫連携API仕様書.xlsx",
      "sha256": "2f59f29339682d186cd3075f53db9de4bf03603eadaf62bf0a2e52a78e7f07dc",
      "version": "2.0",
      "kind": "spec",
      "sheet": "(ブック全体: 3 シート)",
      "cells": "(ブック全体)"
    },
    "text": "[API一覧 B12:D14] エンドポイント メソッド 説明 /v1/inventory GET 在庫数と引当可能数を返す在庫照会API /v1/shipments POST 出荷指示を登録する [制約 A1] 本仕様書の制約事項 [制約 C5:E6] レート制限 600 req/min 超過時は HTTP 429 を返す データ保持期間 13か月 明細データが対象\n\n[制約 C40:E40] 同時接続数 50 IP 単位で計数する\n\n[改訂履歴 A1:C2] 版 日付 内容 2.0 2026-07-30 レート制限を 600 req/min に改訂",
    "chunk_id": "0_16a2d446-8f6f-4aa3-811f-31b394c22a46"
  },
  {
    "file_id": "file-kix-…",
    "filename": "jetuse-spike-prep01-サンプル在庫連携API仕様書.xlsx",
    "score": 0.747,
    "source": {
      "file": "jetuse-spike-prep01-サンプル在庫連携API仕様書.xlsx",
      "sha256": "3da7abfa7455241e27bab43b73d27e039a9feaf7ddd39fe1a13250765847848a",
      "version": "1.0",
      "kind": "spec",
      "sheet": "(ブック全体: 3 シート)",
      "cells": "(ブック全体)"
    },
    "text": "[API一覧 B12:D14] エンドポイント メソッド 説明 /v1/inventory GET 在庫数と引当可能数を返す在庫照会API /v1/shipments POST 出荷指示を登録する [制約 A1] 本仕様書の制約事項 [制約 C5:E6] レート制限 300 req/min 超過時は HTTP 429 を返す データ保持期間 13か月 明細データが対象 [制約 C40:E40] 同時接続数 50 IP 単位で計数する\n\n[改訂履歴 A1:C2] 版 日付 内容 1.0 2026-07-30 レート制限を 300 req/min に改訂",
    "chunk_i
```

- 引用の件数: **2**（0 件なら判定は FAIL）
- 同一ファイル由来の引用が持つ (シート, セル範囲) の種類数: `{'file-kix-045…': 1, 'file-kix-bed…': 1}`
  → **1 種類だけ**であること（= 属性はファイル単位）: **True**

判定: **PASS**

> **この差は隠さない。** 「マネージドでもセル単位で返る」ように見せる実装
> （1 チャンク = 1 ファイルへ無理に分割する等）はしていない。セル単位の出典が要るなら
> `adb` バックエンドを選ぶ、というのが ADR-0020 の決定内容そのものである（可視化は RAGM-03）。
