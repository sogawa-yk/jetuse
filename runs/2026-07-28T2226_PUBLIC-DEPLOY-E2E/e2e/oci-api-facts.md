# 実機で確認した OCI API の事実（レビューでの誤認を防ぐための記録）

## OCI Speech `list_voices` のレスポンス項目
`oci.ai_speech.AIServiceSpeechClient.list_voices(compartment_id=...)` が返す各要素の属性は
（Python SDK 2.183.0 / us-chicago-1 で実測、65件）:

```
voice_id, display_name, description, gender, supported_models,
language_code, language_description, sample_rate_in_hertz, words_per_minute, is_default_voice
```

実例（日本語ボイス）: `voice_id='Yuki'`, `language_code='ja-JP'`, `supported_models=['TTS_2_NATURAL']`。
`model_name` という属性は**存在しない**。よって `jetuse_core/tts.py` の probe が
`voice_id` / `language_code` / `supported_models` を見るのは実レスポンスと一致している。

## リージョン別の TTS 可用性（同日実測・同一テナンシ）
| リージョン | `synthesize_speech` | `list_voices` |
|---|---|---|
| us-chicago-1 | 200（MP3取得） | 200（65件・日本語5ボイスあり） |
| ap-osaka-1 | 404 | 404 |
| ca-toronto-1 | 404 | 404 |

`list_voices` は `compartment_id` を省くとテナンシルート扱いになり、**リソースプリンシパルでのみ 404**。

## GenAI の IAM resource-type 実在確認
policy を1文だけ作成して判定（無効なら `CreatePolicy` が 400 "No permissions found"、
有効なら非ホームリージョンでは "Please go to your home region ..." まで進む）:
- 実在: `generative-ai-response` / `generative-ai-conversation` / `generative-ai-chat` / `generative-ai-model`
- 非実在: `generative-ai-responses`（複数形） / `generative-ai-agent-family`
