# AGT-02 検証レポート: MCPチャット

日付: 2026-06-11
仕様: specs/11-agents.md [AGT-02] / 前提: SPIKE-11
状態: **実機E2E完了**（イメージ0.15.0、migration 006、SPA同時デプロイ）

## SPIKE-11（実機確定）

- Responses APIは **`type:"mcp"` ツール対応**: `server_url` 指定でOCIがリモートMCPサーバーをサーバーサイド呼び出し（`mcp_list_tools` → `mcp_call` アイテム）
- `require_approval:"always"` で `mcp_approval_request` アイテム。継続は **元のuserメッセージ+承認アイテム+`mcp_approval_response`** をinputに（userメッセージ必須 — 欠くと "Input items must contain at least one message"）

## 実装

- `MCP_SERVERS`（owner分離、migration 006）+ CRUD API。URLはhttps必須+SSRF公開ホスト検証
- ツール選択パネルに「MCPサーバー」セクション: 一覧チェックボックス+インライン追加（表示名+URL）/削除
- stream_agent拡張: 選択サーバーを `type:"mcp"` ツールに変換（都度承認=always / 自動=never）。`mcp_call` は実行中通知、`mcp_approval_request` は承認カード（kind=mcp、承認時はexecute-tool不要でapprove/denyフラグのみ継続送信）
- **認証付きサーバーは501で明示拒否**: Vault書き込みが現行ポリシー（secret-family read）で不可のため。`manage secret-family` 追加（人間作業）後に開放 — docs/setup/iam.mdに追記

## 実機E2E（API GW経由、公開MCPサーバー=deepwiki）

| ケース | 結果 |
|---|---|
| サーバー登録/一覧/削除 | OK（owner分離・SSRF/https検証はpytest） |
| 自動実行モード | `mcp_call` 通知→**サーバーサイド完結で正答**（wiki構造「1 Overview」） |
| 承認モード | pending_approvalカード→承認→継続ストリームで正答 |
| pytest | 59件パス（承認継続のmcp_approval_response構築、トークン付き501等） |

## 追補（AGT-02b、ブラウザ実機での切断報告対応）

ユーザー報告「⚠ TypeError: network error」: CIログ解析で**バックエンドは正常**（OCIのMCP付きresponsesが応答ヘッダまで64秒かかる間にクライアント切断 → upstream cancelled, partial_chars=0）。curl経由の同一クエリ5連続は完走 = GW/ブラウザ経路の間欠切断（backlog #12系）。対策: **コンテンツ受信前のネットワーク断はフロントが1回自動リトライ**し、発生時のメッセージに「接続が切断されました。再生成をお試しください」を付加（SPAのみ再デプロイ）。
