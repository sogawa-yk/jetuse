# デモ基盤（SP1〜SP3）統合 E2E — 一次証跡

実施: 2026-08-04 / 実 OCI（us-chicago-1）/ 自分の app スタック
稼働イメージ: `jetuse-dev-api:dev-sogawa-ee142e5` = `internal-dev` の HEAD
セッション ID: `9b4cfeee-4f00-44f6-b432-1a399182984d`（下記すべて同一セッション）
ホスト名は `<apigw-host>` にマスクしてある。

## 実行したコマンド（順に）

```sh
BASE=https://<apigw-host>

# 7a デモ一覧（DB 経路）
curl -sS "$BASE/api/demos"

# 7b セッション生成
curl -sS -X POST "$BASE/api/builder/sessions" -H 'Content-Type: application/json' -d '{}'

# 7c DB から読み戻す
curl -sS "$BASE/api/builder/sessions/9b4cfeee-4f00-44f6-b432-1a399182984d"

# 7d ヒアリング1回目（LLM 構造化出力）
curl -sS -X POST "$BASE/api/builder/sessions/9b4cfeee-4f00-44f6-b432-1a399182984d/messages" \
  -H 'Content-Type: application/json' \
  -d '{"content":"社内の問い合わせ対応を自動化するデモを作りたい。想定利用者は情シスの担当者。"}'

# 7e 必須項目不足のまま設計を試す（仕様どおり弾かれる）
curl -sS -X POST "$BASE/api/builder/sessions/9b4cfeee-4f00-44f6-b432-1a399182984d/design" -H 'Content-Type: application/json' -d '{}'
#   → HTTP 409
#   {"detail":"要求サマリが設計に足りません(missing: industry)。ヒアリングで必須項目を埋めてください(specs/19 §3.1)"}

# 7f 追加ヒアリング
curl -sS -X POST "$BASE/api/builder/sessions/9b4cfeee-4f00-44f6-b432-1a399182984d/messages" \
  -H 'Content-Type: application/json' \
  -d '{"content":"業種は製造業です。扱うデータは社内規程とFAQのPDF。利用者は情シス3名。"}'

# 7g デモ設計
curl -sS -X POST "$BASE/api/builder/sessions/9b4cfeee-4f00-44f6-b432-1a399182984d/design" -H 'Content-Type: application/json' -d '{}'

# 7h 永続化の確認
curl -sS "$BASE/api/builder/sessions/9b4cfeee-4f00-44f6-b432-1a399182984d"
```

## HTTP ステータス（実測）

| # | リクエスト | HTTP | 応答本文 |
|---|---|---|---|
| 7a | GET /api/demos | **200** | [7a-demos.md](./7a-demos.md) |
| 7b | POST /api/builder/sessions | **200** | [7b-create-session.md](./7b-create-session.md) |
| 7c | GET .../\<id\> | **200** | [7c-get-session.md](./7c-get-session.md) |
| 7d | POST .../messages | **200** | [7d-messages-1.md](./7d-messages-1.md) |
| 7e | POST .../design（不足時） | **409** | 上記コマンド欄に応答全文 |
| 7f | POST .../messages | **200** | [7f-messages-2.md](./7f-messages-2.md) |
| 7g | POST .../design | **200** | [7g-design.md](./7g-design.md) |
| 7h | GET .../\<id\> | **200** | [7h-final-session.md](./7h-final-session.md) |

## 前提として流した migration

`internal-dev` の checkout から、アプリが使うシカゴの `jetuse-dev-adb` に対して実行。

```
接続先 DB=..._JETUSEDEV  USER=JETUSE_SOGAWA
適用: 11 件
 + 017_demos_v2  018_demos_idx_owner  019_demos_idx_visibility
 + 020_conversations_demo_id  021_conversations_idx_demo  022_demo_backend_targets
 + 023_dbt_idx  024_rag_files_filename_char
 + 025_builder_sessions  026_builder_sessions_idx  027_builder_sessions_sufficient
```

適用前は `/api/demos` が 503 `database unavailable` だった。**適用が状態を変えた**ことが、
この migration 群が実際に必要だったことの裏づけになっている。
