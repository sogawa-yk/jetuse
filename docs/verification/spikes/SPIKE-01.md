# SPIKE-01: Responses API 基礎検証

実施日: 2026-06-10 / リージョン: ap-osaka-1 / 実行: `spikes/spike01_responses.py`（venv: Python 3.12, openai 2.41.0, oci-genai-auth 1.0.0）

## 目的

openai-python の base_url 差し替えで OCI GenAI の OpenAI互換APIに接続し、①非ストリーミング ②SSEストリーミング ③モデル列挙 ④usage取得 を大阪リージョンの実機で確認。TTFT計測でモデル切替UIのデフォルト候補を決める。

## 手順

1. `~/.oci/config` のIAM署名を `oci_genai_auth.OciUserPrincipalAuth`（httpx auth）で注入、`CompartmentId` ヘッダにコンパートメントOCIDを設定
2. ベースURL 2系統（`/openai/v1`, `/20231130/actions/v1`）で `GET /models` 疎通確認
3. 候補5モデルで Responses API / Chat Completions の非ストリーミング + usage
4. ストリーミングを各モデル×3回、TTFT（最初の `output_text.delta` / content delta まで）と総時間を計測

## 結果

### API対応マトリクス（実測）

| モデル | Responses API | Chat Completions | 備考 |
|---|---|---|---|
| openai.gpt-oss-120b | ✅ | ✅ | フル対応 |
| meta.llama-3.3-70b-instruct | ✅ | ✅ | フル対応（「Meta非対応」というサンプルREADMEの記載に反し動作） |
| google.gemini-2.5-flash | ❌ `Incorrect apiIdentifier GenerativeAiService.responses` | ✅ | Chat Completionsのみ |
| google.gemini-2.5-pro | ❌ 同上 | ✅ | Chat Completionsのみ |
| cohere.command-a-03-2025 | ❌ `Unsupported OpenAI operation` | ❌ 同左 | OpenAI互換API自体が不可。ネイティブAPI（oci SDK chat）が必要 |

### ベースURL / モデル列挙

- `GET /models` は**両パスとも不可**（`/openai/v1`: 404 "Path doesn't map to a registered service" / `/20231130/actions/v1`: 400）。モデル列挙はOpenAI互換APIでは提供されず、**OCI CLI/SDK の `generative-ai model-collection list-models` を使う必要がある**。
- 推論系エンドポイント（responses, chat/completions）は `/openai/v1` で動作。以後これを正規パスとする。

### レイテンシ計測（同一プロンプト・短文・3回平均）

Responses API ストリーミング:

| モデル | TTFT平均(s) | TTFT最小(s) | 総時間平均(s) |
|---|---|---|---|
| openai.gpt-oss-120b | 0.81 | 0.60 | 1.39 |
| meta.llama-3.3-70b-instruct | 0.07 | 0.06 | 0.83 |

Chat Completions ストリーミング:

| モデル | TTFT平均(s) | TTFT最小(s) | 総時間平均(s) |
|---|---|---|---|
| openai.gpt-oss-120b | 0.70 | 0.34 | 1.18 |
| google.gemini-2.5-flash | 4.86 | 3.11 | 5.02 |
| google.gemini-2.5-pro | 14.37 | 10.88 | 14.84 |
| meta.llama-3.3-70b-instruct | 0.08 | 0.07 | 0.91 |

- usage は非ストリーミング・ストリーミング（`stream_options.include_usage` / Responses final response）とも取得可能。
- Gemini系は推論（thinking）が走るためTTFTが大きく、配信もほぼ末尾バースト（total−TTFT が0.2〜0.5s）。**体感ストリーミングが効かない**点はUI設計の考慮事項。
- Responses APIのSSEイベント型はOpenAI準拠（`response.created` / `response.output_text.delta` / `response.completed` 等）を確認。

## 設計への影響

1. **サービス層は2系統サポートが必須**: Responses API（gpt-oss / llama、File Search等のagentic機能用）+ Chat Completions（Gemini系）。Cohere Command Aを使うならOCIネイティブSDK経路の3系統目が要る → モデルカタログに「対応API種別」属性を持たせる。
2. **モデル切替UIのデフォルト候補（大阪）**:
   - 標準/高速: `openai.gpt-oss-120b`（Responses API・agentic対応・TTFT<1s）
   - 軽量: `meta.llama-3.3-70b-instruct`（TTFT 0.07sと最速）
   - 高品質: `google.gemini-2.5-pro`（TTFT 10s超のためUIで「思考中」表示が必須）
   - バランス: `google.gemini-2.5-flash`
3. モデル一覧の動的取得は管理API（OCI SDK）側で実装し、OpenAI互換 `/models` には依存しない。

## 残課題

- command-a-reasoning / command-a-vision のネイティブAPI検証（Phase 2 CHAT-01で）
- 長文生成時のストリーミング安定性（SPIKE-02のAPI GW経由計測と合わせて確認）

## 実行ログ（抜粋）

```
[OK] openai.gpt-oss-120b: 1.18s, out=75chars, usage(in=94, out=130, total=224)
[NG] cohere.command-a-03-2025: BadRequestError: 400 'Unsupported OpenAI operation'
[NG] google.gemini-2.5-flash(responses): 400 'Incorrect apiIdentifier GenerativeAiService.responses'
[OK] meta.llama-3.3-70b-instruct: 0.90s, usage(in=66, out=53, total=119)
[OK] google.gemini-2.5-flash(chat): 3.40s, usage(in=22, out=82)
[OK] google.gemini-2.5-pro(chat): 14.35s, usage(in=22, out=175)
events: response.created / response.in_progress / response.output_item.added /
        response.content_part.added / response.output_text.delta / response.completed
```

全ログ: 実行すれば再現可能（`.venv/bin/python spikes/spike01_responses.py`）
