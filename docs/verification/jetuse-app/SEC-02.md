# SEC-02 検証レポート: ガードレール（入力モデレーション）と監査ログ

- 日付: 2026-06-13 / ブランチ: `task/sec-02` / 仕様: specs/15-hardening.md

## モデレーション方式の実機調査と決定

| 候補 | 実機結果 | 判定 |
|---|---|---|
| 互換APIの `/moderations` | **404**（Path doesn't map） | 不可 |
| cohere `safety_mode` | **cohereはchat completions自体が「Unsupported OpenAI operation」**（新quirk） | 不可 |
| **LLM自己判定ガード（採用）** | llama-3.3-70b（高速）で5カテゴリ判定 → 遮断時はSSEエラー | 採用 |

- `MODERATION_ENABLED=true` で有効（**既定false**。有効化は+0.5〜1秒/メッセージ — 採否はユーザー判断）
- 判定失敗時は通す（可用性優先・ログのみ）。チャット全経路（usecase/エージェント含む）の入口で適用
- E2E: 不適切入力→「利用ポリシーに抵触」エラー+監査記録（moderation_block/カテゴリ） / 正常入力→通過

## 監査ログ（誰が・どの機能・どのモデル・トークン数）

- migration 011 `AUDIT_LOG` + `jetuse_core/audit.py`（ベストエフォート記録・集計）
- 記録ポイント: chat/agent/rag（3エンジンとも）・nl2sql・dbchat(fn)・minutes・tts(CI/fn)・stt・
  usecase/video/voicechat（`ChatRequest.source` ラベルでUIから付与）・moderation_block
- **chat completions系のトークン欠落を解消**: `stream_options.include_usage` がOCIで動作することを
  確認し全モデルで記録（gpt-oss: 70/309、llama: 41/2 等を実測）
- 集計API `GET /api/admin/usage?days=N`: `ADMIN_USERS`（カンマ区切りsub）のみ200、他は403
  （本番は daybreaks.link@gmail.com を設定済み。M2Mクライアントで403を実測）

## デプロイと教訓

- CI 0.26.0 + fnルーター0.1.2。**ディスク95%でpushが再び不完全になりかけたが、
  「apply前のレジストリ側検証」（前回障害の教訓）で検出し、障害ゼロで再push**
- fnはイメージ更新後の初回invokeがpull込みで36秒（一度きり。以降は通常のコールド2秒級）
- 残り: OPS-01（このAPIを使う管理画面）/ SEC-03 / OPS-02
