-- 取込定義の出所追跡(ADR-0013 D6 / PLG-02): agents に source_plugin_id / source_version。
-- 対象表 agents は 007_agents.sql が作成済み(JetUse アプリ表)。014_usecase_source.sql と分離し、
-- 1 ファイル = 1 ALTER = schema_migrations 1 記録にすることで、片方成功・片方失敗からの
-- 再実行(ORA-01430 ループ)を避ける。冪等性は migrate ランナーの適用済み版スキップが担保。
-- 幅は manifest の MAX_ID_LEN(255) / MAX_VERSION_LEN(64) と一致させる。
ALTER TABLE agents ADD (source_plugin_id VARCHAR2(255), source_version VARCHAR2(64))
