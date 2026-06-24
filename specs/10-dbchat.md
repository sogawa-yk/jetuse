# specs/10 — Phase 5 DBチャット/NL2SQL（SQL-01〜04）

状態: ドラフト（2026-06-11作成。SQL-01/02を先行）
仕様参照: SPIKE-04（SQL Search 10/10実証・Select AI NL2SQL比較）/ docs/setup/iam.md（動的グループ整備済み）

## バックエンド方針

**SQL Search（SemanticStore + generateSqlFromNl）を主バックエンドに採用**（SPIKE-04: 10/10 vs Select AI NL2SQLのfiscal四半期混同等）。Select AI直接実行モードはSQL-04でオプション化し比較ドキュメントに反映。

## [SQL-01] セットアップ自動化

- `ops/setup-sql-search.py`（冪等）: JETUSE_QUERYユーザー（CREATE SESSIONのみ） → ウォレットsecret（mTLS dbtools用、Vault流用） → DBTools接続2本（enrich=ADMIN / query=JETUSE_QUERY、SSOキーストア） → SemanticStore（schemas=SH） → enrichment（FULL_BUILD）ポーリング
- enrichボディはSDK `oci.generative_ai_data.models.GenerateEnrichmentJobDetails` 準拠（API文書なし）
- 完了条件: enrichment SUCCEEDED + 日本語10問の生成・実行成功（spike04_sql_search.py再実行）

## [SQL-02] NL2SQLチャットフロー

- `POST /api/chat/nl2sql`（SSE。生成は実測30秒前後 → keepalive必須のため/api/chat配下=300sルート）
  - body: `{"question": str}` → events: keepalive… → `{"sql": str}` → `[DONE]`（生成のみ。実行しない）
- `POST /api/dbchat/execute` body: `{"sql": str}` → `{columns, rows, row_count, truncated}`
  - **ガード**: JETUSE_QUERY接続（CREATE SESSIONのみの読取専用ユーザー）/ SELECT・WITH以外を拒否（コメント・セミコロン除去後に判定）/ `FETCH FIRST 200 ROWS` 強制 / call_timeout 30s / 値は文字列化して返す
- UI `/dbchat`: 質問入力 → 生成中プログレス → **生成SQLを表示しユーザーが確認・編集 → 実行ボタン** → 結果テーブル（件数・打ち切り表示）。SQLはコピー可
- 会話履歴・ADB永続化はPhase 5出口で判断（まず単発フロー）

## [SQL-03] 結果の自動グラフ化（着手時に追記）

## [SQL-04] Select AI直接実行モード + 比較ドキュメント（着手時に追記）

- docs/comparison/nl2sql-backends.md（SQL Search vs Select AI NL2SQL vs 素のLLM。SPIKE-04の手法を再利用）

## 完了条件（人間チェックポイント③）

顧客デモ可能品質。日本語10問の正答率を定点指標化（SQL-01で再確立済み: 10/10）
