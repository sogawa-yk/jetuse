# 比較: リアルタイム翻訳の方式(OCI Language vs Enterprise AI LLM) — SPIKE-E5

ENH-10(リアルタイム文字起こし→翻訳)の翻訳バックエンド。両方とも ap-osaka-1 可用・低レイテンシ(実機)。

| 方式 | レイテンシ(実測) | 品質 | 依存/IAM | 特徴 |
|---|---|---|---|---|
| **OCI Language**(batch translate) | ~0.12s/文(2文0.25s) | 良(翻訳専用) | CIのRPに `use ai-service-language-family` 要 | 翻訳特化・最速・多言語。文脈は持たない |
| **Enterprise AI LLM**(llama-3.3-70b) | ~0.2–0.5s/文 | 良(文脈考慮可) | 既存 generative-ai 権限で動作(追加IAM不要) | 既存基盤・柔軟。やや遅い |

## 採用
- **既定=LLM**(追加IAM不要で即動作)。**OCI Language も選択可**(より高速・翻訳特化)だが
  CIのリソースプリンシパルに `use ai-service-language-family` のIAMが必要(未付与だと当該方式のみ502)。
- UIで方式を切替可能(realtime画面)。文字起こし(Whisper)→確定テキストを逐次翻訳して原文/訳文を併記。

## 実機根拠
JP→EN: LLM 0.20–0.54s/文、OCI Language 0.12s/文(いずれも自然な訳)。docs/verification/ENH-10.md。
