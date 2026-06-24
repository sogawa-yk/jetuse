# CHAT-01/03 検証レポート: ストリーミングチャットE2E

日付: 2026-06-10
仕様: specs/07-chat.md
状態: **サーバー側E2E完了・UI配信済み。ブラウザでの操作確認は人間レビュー待ち**

## 実行結果

| チェック | 結果 |
|---|---|
| pytest（9件: レジストリ/SSE整形/認可/モデル一覧/不明モデル400） | passed |
| ruff / eslint / vite build | クリーン |
| ローカル実LLM | gpt-oss-120b（Responses）/ llama-3.3-70b（Chat Completions）/ gemini-2.5-flash（同）すべてストリーミング成功、usage取得 |
| **実機E2E（API GW経由）** | JWT付き `POST /api/chat/stream` で**gpt-ossのストリーミング成功**。コンテナは**リソースプリンシパル署名**でGenAI呼び出し（`AUTH_MODE=resource_principal` — IAM作業で作成いただいた動的グループが機能） |

## 実機で確定した未文書仕様（SPIKE-01からの追加発見）

1. **Responses APIは `OpenAi-Project` ヘッダ必須**（無いと「Compartment ID must be provided」or「Non-OpenAI models require...」— エラー文言が紛らわしい）
2. **Responses APIのinputは `{role, content:"str"}` 形式を拒否**。受理されるのは文字列全体、または `{"type":"message","role":...,"content":[{"type":"input_text"|"output_text","text":...}]}` の型付き形式のみ
3. **llama-3.3-70bはResponses APIで404になる**（SPIKE-01時点から挙動変化）。Chat Completionsでは正常 → レジストリの `api` 属性で吸収（モデル別API系統の設計が正解だった）

## 実装内容

- `jetuse_core/models.py`: モデルレジストリ（gpt-oss-120b標準 / llama高速 / gemini-2.5-pro高品質 / gemini-2.5-flash）
- `jetuse_core/chat.py`: 2系統APIを `{"delta"}/{"usage"}/{"error"}` イベント列に正規化。接続失敗1リトライ、usage構造化ログ
- `service`: `POST /api/chat/stream`（SSE、**無通信15秒でkeepalive** — gemini-proのTTFT対策）、`GET /api/chat/models`
- UI（CHAT-03）: ストリーミング表示・停止（AbortController）・再生成・コピー・Markdown（react-markdown+remark-gfm）・モデル切替・Shift+Enter改行

## 既知の制約（次タスク以降）

- クライアント停止時に上流LLMストリームを即時キャンセルしない（トークン消費が完走する）
- 会話はリロードで消える（CHAT-02のADB永続化で解消）
- シンタックスハイライト・Mermaid描画は未実装（CHAT-03b）
