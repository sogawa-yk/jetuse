# GET .../<id> — 設計結果の永続化を確認

```json
{
  "id": "9b4cfeee-4f00-44f6-b432-1a399182984d",
  "status": "designed",
  "transcript": [
    {
      "role": "user",
      "content": "社内の問い合わせ対応を自動化するデモを作りたい。想定利用者は情シスの担当者。"
    },
    {
      "role": "assistant",
      "content": "業種はどのあたりを想定していますか？また、問い合わせ対応に使う文書（マニュアル・FAQなど）の種類を教えていただけますか？"
    },
    {
      "role": "user",
      "content": "業種は製造業です。扱うデータは社内規程とFAQのPDF。利用者は情シス3名。"
    },
    {
      "role": "assistant",
      "content": "以下の内容で要求サマリをまとめました。問題なければこのままデモ作成を進めます。\n・業種: 製造業\n・ユースケース: 社内の問い合わせ対応自動化（情シス担当者向け）\n・文書: 社内規程とFAQのPDF\n・能力: rag.search"
    }
  ],
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
  "plan": {
    "plan_version": 1,
    "title": "製造業向け社内問い合わせ自動化デモ",
    "description": "情シス担当者向けに、社内規程とFAQをRAGで検索して問い合わせに即答するデモ。製造業の業務文書に基づく回答生成を体験できます。",
    "capabilities": [
      "rag.search"
    ],
    "screens": [
      {
        "id": "inquiry_rag",
        "title": "社内規程検索",
        "blocks": [
          {
            "type": "rag.search",
            "title": "規程・FAQ検索",
            "suggested_prompts": [
              "製造ラインの安全基準を教えて",
              "品質管理マニュアルの主なポイントは",
              "有給休暇の取得ルールを教えて"
            ]
          }
        ]
      }
    ],
    "data": {
      "tables": [],
      "documents": [
        {
          "filename": "shain_kitei_faq.md",
          "title": "社内規程とFAQ",
          "outline": "1. 就業規則 2. 安全衛生規程 3. 品質管理規程 4. よくある質問"
        }
      ]
    }
  },
  "demo_id": null,
  "demo_status": null,
  "created_at": "2026-08-04T12:53:37.180242",
  "updated_at": "2026-08-04T12:55:03.331384"
}
```
