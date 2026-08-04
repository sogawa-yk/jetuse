# POST .../messages — LLM 構造化出力（1回目）

```json
{
  "reply": "業種はどのあたりを想定していますか？また、問い合わせ対応に使う文書（マニュアル・FAQなど）の種類を教えていただけますか？",
  "requirements": {
    "industry": null,
    "use_case": "社内の問い合わせ対応の自動化",
    "capabilities_hint": [
      "rag.search"
    ],
    "data_profile": {
      "documents": "情シス向けの内部マニュアル・FAQ等",
      "tables": null
    },
    "notes": "情シス担当者向け"
  },
  "sufficient": false,
  "missing": [
    "industry",
    "data_profile.documents"
  ]
}
```
