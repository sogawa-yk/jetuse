# SQL-01 検証レポート: SQL Searchセットアップ自動化（jetusedev 26ai）

日付: 2026-06-11
仕様: specs/10-dbchat.md [SQL-01]
状態: **完了**（enrichment SUCCEEDED + 日本語10問 10/10）

## 実施内容

スパイクADB削除に伴い、`ops/setup-sql-search.py`（冪等）でjetusedev 26ai上にSQL Search一式を再構築:

1. `JETUSE_QUERY` ユーザー作成（CREATE SESSIONのみ。SHサンプルはPUBLIC公開のため追加GRANT不要 — jetusedev 26aiにもSH同梱を確認、SALES 918,843行）
2. ウォレットsecret `jetuse-dev-wallet-sso`（cwallet.ssoのbase64。**mTLS必須ADBへのDBTools接続はSSOキーストアsecretで可能と実証**）
3. DBTools接続 `jetuse-dev-dbconn-enrich`(ADMIN) / `jetuse-dev-dbconn-query`(JETUSE_QUERY) — **validate両方OK**。パスワードsecretは既存Vault（jetuse-spike-vault）を流用
4. SemanticStore `jetuse-dev-semstore`（schemas=SH）→ ACTIVE
5. enrichment FULL_BUILD → **SUCCEEDED（315秒）**

## 定点指標（日本語10問、SPIKE-04質問セット）

`spikes/spike04_sql_search.py`（jetusedev向けに接続修正）: **生成・実行成功 10/10**（spikes/data/spike04_results_sql_search.json更新）

## ハマりどころ（tips.mdにも記録）

- enrichのリクエストボディはAPI文書がなく、SDK `oci.generative_ai_data.models` から確定: `{displayName, enrichmentJobType: "FULL_BUILD", enrichmentJobConfiguration: {enrichmentJobType, schemaName}}`（`jobType` 等の誤フィールドは "Content parsing error!" だけ返る）
- semantic-storeのlistコマンドは `oci generative-ai semantic-store-collection list-semantic-stores`（semantic-store配下にlistはない）
- dbtoolsのパスワード指定は `--user-password-secret-id`（複合型ではない）

## .env更新

DBTOOLS_ENRICH_OCID / DBTOOLS_QUERY_OCID / SEMSTORE_OCID をjetusedev版に更新済み
