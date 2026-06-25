-- 取込定義の出所追跡(ADR-0013 D6 / PLG-02): usecases に source_plugin_id / source_version。
-- 対象表 usecases は 004_usecases.sql が作成済み(JetUse アプリ表。CLAUDE.md の「参照のみ」
-- 既存リソース[VCN develop / インスタンス dev / バケット]ではない)。列追加は PLG-02 受け入れ条件。
-- agents への同等列追加は 015_agent_source.sql に分離する(片方失敗時の再実行可能性のため、
-- 1 ファイル = 1 ALTER = schema_migrations 1 記録)。
-- 幅は manifest の MAX_ID_LEN(255) / MAX_VERSION_LEN(64) と一致させる。
ALTER TABLE usecases ADD (source_plugin_id VARCHAR2(255), source_version VARCHAR2(64))
