# シナリオ3 — 対照: テキスト層のある PDF は **OCR を通らない**

シナリオ1 と**同じ内容**をテキスト層つきで作った PDF（`jetuse-spike-prep03-設備点検報告書デジタル.pdf`）を、
同じ 2 つの口（抽出 `POST /api/extract` と取り込み `POST /api/rag/files`）へ通した。

判定根拠は**実際の OCR 呼び出し回数**である（結果だけ見ても「通ったか」は分からない）。
`docunderstand.ocr` / `ocr_vlm` を数えるラッパで包み、この 2 回の操作の前後で数えた。

- 抽出（`POST /api/extract`）中の OCR 呼び出し: **0 回**
- 取り込み（`POST /api/rag/files`）中の OCR 呼び出し: **0 回**
- どちらも 0 回（= 課金していない）: **True**

## それでも本文は従来どおり取り込まれている

- 抽出のチャンク数: **2** / 先頭の出典: `p.1`
- 取り込み状態: **completed** / バックエンド: `{'vector_store': 'indexed', 'select_ai': 'pending', 'opensearch': 'disabled', 'adb': 'indexed'}`

```
9f84d3c8-7707-40c5-b755-1d02cd55121d-1 | sheet=p.2 | 是正処置および交換部品...
9f84d3c8-7707-40c5-b755-1d02cd55121d-0 | sheet=p.1 | 架空商事株式会社  設備点検報告書...
```

- 本文の語 `BRG-7781` が検索で引ける: **True**

判定: **PASS**

> 判定根拠（ページごとに `extract_text()` が空白以外を返すか）は
> `packages/api/tests/test_extract_scan.py` の
> `test_pdf_with_a_text_layer_never_calls_ocr` でも固定してある。
