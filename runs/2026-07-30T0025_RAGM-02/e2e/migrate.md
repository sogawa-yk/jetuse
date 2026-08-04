# マイグレーション適用（deploy 相当）

`python -m jetuse_core.migrate` を検証スキーマ `JETUSE_RAGM02_40D75E` に対して実行した。

```
applied: ['001_init', '002_presets', '003_oci_conversation', '004_usecases', '005_rag', '006_mcp_servers', '007_agents', '008_agent_auto_tools', '009_minutes', '010_agent_framework', '011_audit', '012_normalize_framework', '013_installed_plugins', '014_usecase_source', '015_agent_source', '016_demos', '017_rag_adb', '018_rag_adb_docs', '019_rag_adb_ingest']
```

RAGM-02 が足した 3 表の存在確認（`user_tables`）:

```
RAG_ADB_CHUNKS
RAG_ADB_DOCS
RAG_ADB_INGEST
```

- `017_rag_adb.sql`（チャンク表）/ `018_rag_adb_docs.sql`（版のロック用レジストリ）/
  `019_rag_adb_ingest.sql`（取り込み状態・file_id 単位）は**それぞれ CREATE TABLE 1 文だけ**。
  Oracle の DDL は暗黙コミットなので、1 ファイルに複数 DDL を並べると途中失敗時に
  「表はあるが migration 未記録」で再実行不能になる。
- 索引は `rag_adb.ensure_indexes()` が冪等に作る（マイグレーションには置かない）。
