# シナリオ2（③ の出典粒度）— PASS

実環境: シナリオ1 と同じ実 ADB / スキーマ `JETUSE_SPIKE_M1`。

## 実行コマンド

```
PYTHONPATH=spikes/spike_m1 .venv/bin/python spikes/spike_m1/method_c_own_index.py
```

生ログ全文: `method-c-own-index.log`（③-4 / ③-6 節）

## 期待

同じ検索で、各ヒットに `file` / `version` / `sheet` / `cells` が
**本文への埋め込みではなく列 / JSON の構造化値として**返ること。

## 実結果

検索結果は列としてそのまま返っている（③-4 B の 1 行目）:

```
CHUNK_ID | DOC_FILE | DOC_VERSION | SHEET_NAME | CELLS | KIND | CURRENT_VERSION | SHA256_HEAD | ATTR_SOURCE | DIST | BODY_HEAD
c01 | サンプル在庫連携API仕様書.xlsx | 2.0 | API一覧 | B12:F12 | spec | Y | d134954f8ee0 | spike-m1-fixture | 0.2697 | 在庫照会API GET /v2/inventory は…
```

同じ行を JSON として組み立てても等価（③-6）:

```json
{
  "chunk_id" : "c05",
  "file" : "サンプル在庫連携API仕様書.xlsx",
  "version" : "2.0",
  "sheet" : "制約",
  "cells" : "C5:E5",
  "sha256" : "eaa8b7f54831e2e59d8959511dc639d2829f21ccbbf825f4e828f5213eeead27",
  "kind" : "constraint",
  "current_version" : "Y"
}
```

`body`（本文）列にはメタデータ文字列は含まれていない（本文は `在庫照会API GET /v2/inventory は…`）。
出典は本文と独立した列 / JSON として保持・返却されている。

加えて `attributes` は `JSON` 列で、`JSON_VALUE(attributes, '$.source')` が
`spike-m1-fixture` を返している＝列に無い任意キーもスキーマレスに同居できる。
