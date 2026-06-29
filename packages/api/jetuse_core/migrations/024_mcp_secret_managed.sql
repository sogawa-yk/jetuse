-- BE-08: 認証付き MCP のアプリ管理 secret を識別する provenance 列。
-- secret_managed=1 はアプリ(create_server)が Vault に作成した secret = 削除時に削除予約対象。
-- 0 は secret 無し/外部管理 OCID（誤って外部 secret を削除しないため区別する）。
ALTER TABLE mcp_servers ADD secret_managed NUMBER(1) DEFAULT 0 NOT NULL
