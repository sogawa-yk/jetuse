# ADR-0003: SSEストリーミング経路はAPI Gateway経由とする

日付: 2026-06-10
状態: 承認済み（2026-06-10 人間チェックポイント①）
根拠: docs/verification/SPIKE-02.md

## 決定

ブラウザ→FastAPIのSSEストリーミングは **OCI API Gateway経由** とする。LB直結構成は採用しない。

## 理由（実測）

- API GWはchunked/SSEをバッファリングせず即時フラッシュ（相対遅延 max 0.013s）
- `readTimeoutInSeconds=300` は読み取り間隔のタイムアウトで、総時間は無制限（330秒連続を実証）
- 認証・レート制限・CORS・IP制限をGWレイヤに集約できる（Phase 8の要件を先取り）

## 実装上の必須事項

1. デプロイ仕様で `readTimeoutInSeconds: 300`（デフォルト10のままだと切断される）
2. LLM思考中などの無通信対策に、サーバーから15〜30秒間隔でSSEコメント（`: keepalive`）を送出
3. API GW用NSGに443 ingressを付与（Terraformモジュールの標準部品にする）

## 例外

WebSocket（リアルタイムSTT等）はAPI GW対象外。音声系はクライアント→OCI Speechリアルタイムエンドポイント直接続（短命セッショントークン使用）を第一候補としてVOICE-02で設計する。
