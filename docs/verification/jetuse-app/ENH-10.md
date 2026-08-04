# ENH-10: リアルタイム文字起こし → リアルタイム翻訳

実施日: 2026-06-15 / SPIKE-E5(go: 両方式 大阪可用・低レイテンシ)

## 実装
- jetuse_core/translate.py: `translate(text, target, source, backend)`。backend=llm(llama-3.3-70b)/
  oci_language(AIServiceLanguageClient batch)。LANGUAGES(en/ja/zh/ko/es/fr/de)。
- API: `POST /api/translate`、`GET /api/translate/options`(言語/方式一覧)。
- realtime.tsx: 「文字起こし＋翻訳」へ。確定テキストごとに翻訳し原文/訳文を併記。
  翻訳表示トグル・翻訳先・方式(LLM/OCI Language)を選択。設定はrefで保持しSSE中も反映。
  ナビ/タイトルを「リアルタイム翻訳」に改称。

## 検証
- 翻訳ローカル実機: LLM(en/zh)・OCI Language(en)とも自然な訳を確認(SPIKE-E5)。
- build/lint/ruff グリーン。API 0.41.0 / SPAデプロイ。translate endpoint 401(認証ゲート=稼働)。

## 留意
- 既定=LLM(追加IAM不要)。OCI Language方式はCIのRPに `use ai-service-language-family` が必要
  (未付与時は当該方式のみ502。LLMにフォールバックして利用可)。
