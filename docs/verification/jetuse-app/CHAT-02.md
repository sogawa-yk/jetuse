# CHAT-02 検証レポート: 会話永続化（ADB）

日付: 2026-06-10
仕様: specs/07-chat.md §CHAT-02（案A: CI同居 — ユーザー承認済み）
状態: **実機E2E完了**

## 実行結果

| チェック | 結果 |
|---|---|
| pytest（13件: CRUD / ストリーム保存 / **所有者分離**(他人の会話は404) / 既存回帰） | passed |
| ruff / eslint / build | クリーン |
| マイグレーション | `python -m jetuse_core.migrate` → `001_init` 適用（CONVERSATIONS / MESSAGES / USAGE_LOG / SCHEMA_MIGRATIONS） |
| **実機E2E（API GW経由・JWT付き）** | 会話作成 → 永続化チャット（gpt-oss実応答）→ **再取得でuser/assistant両メッセージ復元** → 一覧1件 → 削除 → 404 |
| コンテナのウォレット取得 | **リソースプリンシパルで非公開バケット（jetuse-dev-app-data）からウォレットzipを起動時取得** → oracledb接続プール生成。動作確認済み |

## 構築・実装内容

- DB: `jetusedev` に専用ユーザー `JETUSE_APP`（CREATE SESSION/TABLE/SEQUENCEのみ、自スキーマ運用。ADMINはマイグレーション時のみ）。mTLSウォレットは生成→非公開バケット配置
- マイグレーション機構: `jetuse_core/migrations/*.sql` を辞書順適用、`SCHEMA_MIGRATIONS` で記録
- API: `GET/POST /api/conversations`, `GET/DELETE /api/conversations/{id}`, `POST /api/chat/stream` に `conversation_id`（永続化）と `persist_user`（再生成時の二重保存防止）を追加。**所有者分離はすべてSQLのWHERE owner_subで強制**
- 永続化はベストエフォート（DB障害時もチャット自体は止めない設計。ログに記録）
- API GW: `/api/conversations` 系ルートを追加（モジュール更新。当初ルート漏れで静的配信に落ちるバグを実機で発見→修正）
- UI: チャットページに履歴サイドバー（新しい会話 / 一覧 / 切替復元 / 削除）。初回送信時に会話自動作成（タイトル=先頭30字）

## ドキュメント反映（ユーザー指示）

- plan.md: **CHAT-06（短期メモリ統合）/ AGT-05（長期メモリ統合）を必須タスク化**
- ADR-0002追記: メモリ機能採用方針とstore=false運用

## 残課題

- [ ] usage一覧の管理UI（Phase 8の利用量集計と合わせて）
- [ ] パスワード類のVault移行（現状はコンテナ環境変数。Phase 8）
- [ ] CHAT-05: タイトル自動生成（LLM要約）・検索。CHAT-06: 短期メモリ統合
