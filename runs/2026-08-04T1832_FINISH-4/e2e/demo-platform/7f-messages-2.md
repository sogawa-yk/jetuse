# POST .../messages — 追加ヒアリングで sufficient=true

```json
{
  "reply": "以下の内容で要求サマリをまとめました。問題なければこのままデモ作成を進めます。\n・業種: 製造業\n・ユースケース: 社内の問い合わせ対応自動化（情シス担当者向け）\n・文書: 社内規程とFAQのPDF\n・能力: rag.search",
  "requirements": {
    "industry": "製造業",
    "use_case": "社内の問い合わせ対応を自動化",
    "capabilities_hint": [
      "rag.search"
    ],
    "data_profile": {
      "documents": "社内規程とFAQのPDF",
      "tables": null
    },
    "notes": "利用者は情シス3名"
  },
  "sufficient": true,
  "missing": []
}
```
