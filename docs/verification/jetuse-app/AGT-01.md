# AGT-01 検証レポート: Function Callingフレームワーク

日付: 2026-06-11
仕様: specs/11-agents.md [AGT-01] / 前提: SPIKE-09
状態: **実機E2E完了**（イメージ0.13.0、SPA同時デプロイ）。UI操作感はユーザーレビュー待ち

## 実装

- **ツールレジストリ**（jetuse_core/tools.py）: `web_search`（DuckDuckGo HTML版スクレイプ、APIキー不要）/ `web_fetch`（UC-02のSSRFガード付き本文抽出を流用）/ `code_interpreter`（OCI built-in透過）。execute-toolはレジストリ名+JSON Schema検証済み引数のみ受理
- **エージェントループ**（chat.py stream_agent）: ステートレス・最大5ホップ。SSEイベント `tool_call`（label/arguments/call_id/status）と `tool_result`（preview）を追加
- **承認2モード**: 都度承認（既定）=pending_approvalでストリーム終了→UI承認カード→`POST /api/agent/execute-tool`→tool_results付きで継続呼び出し / 自動実行=サーバー側でループ
- UI: チャットの🛠トグル（Responses系モデル選択時のみ表示）+ 自動実行チェック + 承認カード（承認して実行/拒否）

## 実機E2E（API GW経由、イメージ0.13.0）

| ケース | 結果 |
|---|---|
| ツール単体（web_search「OCI リージョン数」） | 実検索結果5件（docs.oracle.com等） |
| 自動実行マルチホップ「2026年6月の祝日をWebで確認」 | **search→fetch→正答（6月に祝日なし）** |
| 承認モード1段目 | `pending_approval` でストリーム停止 |
| 承認→実行→継続 | 継続ストリームが次のツール（web_fetch）の承認待ちに正しく遷移（多段承認が機能） |
| code_interpreter（「フィボナッチ30番目」） | built-in実行で正答 |
| ガード（pytest） | 未知ツール/不正引数/型不正/built-in直接実行の拒否（53件パス） |

## 既知の挙動（モデル起因）

- ツールがエラーを返した直後の最終回答が空になることがある（gpt-oss挙動。既存の空応答注記がUIに出る。再生成で回復）
- web_searchはDuckDuckGo HTML版に依存（レート制限・構造変更リスク）。本番では検索APIへの差し替えを推奨（ToolDefの差し替えのみで可能な構造）

## 残（Phase 6）

AGT-02（MCP）/ AGT-03（Agent Builder）/ AGT-04（Applications/Deployments）/ AGT-05（長期メモリ・必須）

## 追補（AGT-01b、ユーザーフィードバック対応）

🛠ボタンを「ツール選択パネル」（⚙パネルと同形式）に変更: `GET /api/agent/tools` の一覧をチェックボックスで選択（説明・built-inバッジ付き、自動実行チェックもパネル内へ）。選択数が🛠バッジに表示され、**チェックしたツールだけがモデルに渡る**（`enabled_tools` でtools配列をフィルタ）。実機: `enabled_tools:["web_search"]` 指定でweb_searchのみ使用され正答（イメージ0.13.1）。
