# PREP-03 実環境 E2E サマリ

実行環境: 共有 loop ADB の run 固有スキーマ `JETUSE_PREP03_EE2FB4` / dev コンパートメント /
OCI Document Understanding（既定エンジン）・OCI Generative AI（埋め込み・生成）は実物。

- PASS — シナリオ0（抽出口・頁つき / 上限 422）
- PASS — シナリオ1（スキャン PDF が検索でヒット・引用に頁）
- PASS — シナリオ2（画像が検索でヒット）
- PASS — シナリオ3（対照: テキスト層のある PDF は OCR を通らない）

## OCR 呼び出しの全記録（このセッション）

```
[
  {
    "engine": "ocr",
    "bytes": 134189,
    "pages": 2,
    "seconds": 3.9,
    "mean_confidence": 0.991
  },
  {
    "engine": "ocr",
    "bytes": 134189,
    "pages": 2,
    "seconds": 3.3,
    "mean_confidence": 0.991
  },
  {
    "engine": "ocr",
    "bytes": 62516,
    "pages": 1,
    "seconds": 2.0,
    "mean_confidence": 0.9741
  }
]
```

> スキャン PDF 2 ページ = 1 回 / 画像 1 枚 = 1 回。テキスト層のある PDF では 1 度も呼ばれない。
