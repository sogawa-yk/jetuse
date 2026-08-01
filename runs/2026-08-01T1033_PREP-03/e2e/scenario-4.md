# シナリオ4（付録）— 明示指定した VLM エンジンが実機で通る

既定は Document Understanding（シナリオ0〜3 はすべて DU）。利用者が
`ocr_engine=vlm` を明示したときだけ VLM（ビジョン LLM）へ切り替わる。**自動では切り替えない**。

同じ画像 `jetuse-spike-prep03-受入検査記録.png` を `POST /api/extract` へ `ocr_engine=vlm` で渡した:

- HTTP ステータス: **200** / チャンク数: **1**
- 呼ばれたエンジン: `['ocr_vlm']`（`ocr_vlm` = VLM 経路）: **True**
- 出典がページ番号: **True**
- 本文を読めている（`LOT-2026-0518` と判定 `合格`）: **True**

```
架空製作所 受入検査記録
ロット番号: LOT - 2026 - 0518
検査項目: 外径寸法 42.0 mm ± 0.05
判定: 合格 (測定値 41.98 mm)
検査員: 品質保証部 佐藤
```

呼び出しの記録:

```
[
  {
    "engine": "ocr_vlm",
    "bytes": 62516,
    "pages": 1,
    "seconds": 13.7,
    "mean_confidence": null
  }
]
```

判定: **PASS**

> VLM は**ページごとに LLM を呼ぶ**ので、ページ数に比例してコストが掛かる。
> 既定にしない理由は `docs/verification/PREP-03.md`（エンジン既定の判断根拠）。
