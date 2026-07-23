# ADR-0002: 会話状態の管理方式（履歴の正はADB）

日付: 2026-06-10
状態: 承認済み（2026-06-10 人間チェックポイント①）
根拠: docs/verification/spikes/SPIKE-05.md

## 背景

OCI Responses APIにはサーバー側会話状態（Conversations）があり、アプリ側でADBに履歴を持つ設計と二重管理になる懸念があった。

## 決定

1. **履歴の正（source of truth）はADB**。会話一覧・検索・タイトル・共有・監査はADBのデータで実現する。OCIのConversations APIには一覧機能がなく（実機確認済み）、保持期間も未文書化のため、表示用データをOCI側に依存しない。
2. **OCI Conversationsは実行時コンテキストとして利用する**。毎ターン全履歴を再送せずに済み、入力トークンを節約できる。ADBのCONVERSATIONSレコードに `oci_conversation_id` を対応付ける。
3. **OCI Conversationが失われても会話を継続できる**こと（ADB履歴から `input` を再構築するフォールバック）をCHAT-01の要件とする。
4. **GenerativeAiProjectを分離単位とする**。初期実装はアプリ全体で1 Project。エンタープライズ向けにはテナント単位のProject分割をオプション化（Projects間の完全分離は実機確認済み）。
5. 会話削除時はADBとOCI側の両方を削除する。

## 影響

- CHAT-02のADBスキーマに `oci_conversation_id`、`oci_project_id` カラムを追加
- usage（トークン数）はレスポンスから毎ターンADBに記録（SEC-02の監査ログ要件を先取り）

## 追記（2026-06-10 ユーザー指示）

本ADRの「履歴の正はADB」は維持しつつ、**Enterprise AI Agentsのメモリ機能を後続フェーズで必ず採用する**:

- **短期メモリ（CHAT-06）**: Conversations + 履歴圧縮をトークン削減・レイテンシ改善に利用。ADB会話レコードにconversation idを紐付け
- **長期メモリ（AGT-05）**: `subject_id`（JWTのsub）で会話横断パーソナライズ

UI（一覧・削除・集計）の正はADBのまま変わらない。なお、Responses APIは `store` 未指定でサーバー側保存される実機挙動のため、メモリ統合の設計まではアプリは `store=false` を明示する（CHAT-01で対応済み）。
