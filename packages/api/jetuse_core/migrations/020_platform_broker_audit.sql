-- Platform API ブローカーのアクセス監査(PAPI-01 / ADR-0014 §5)。
-- 全アクセス(許可 ALLOW / 拒否 DENY)を記録し、越境の試行が必ず監査に残ることを保証する(plan §12)。
-- 既存 audit_log(機能別集計)とは別表。ブローカー固有の軸(tenant/plugin/scope/decision/jti)を持つ。
-- tenant/plugin_id/scope/jti は ASCII(OCID・manifest id・スコープ語彙・hex)なので幅は BYTE/CHAR 同値。
-- reason/resource は日本語の拒否理由・非ASCII リソース名を含みうるため CHAR セマンティクスで確保する。
-- 幅は record_broker_access の切り詰め上限(plugin/tenant=255, scope/jti=64, decision=8, reason/resource)と一致。
-- 列名 resource_id は Oracle 予約語 RESOURCE を避けたもの(裸の resource は ORA-03050 になる)。
CREATE TABLE platform_broker_audit (
  id VARCHAR2(36) PRIMARY KEY,
  tenant VARCHAR2(255) NOT NULL,
  plugin_id VARCHAR2(255) NOT NULL,
  scope VARCHAR2(64) NOT NULL,
  decision VARCHAR2(8) NOT NULL,
  reason VARCHAR2(200 CHAR),
  resource_id VARCHAR2(255 CHAR),
  jti VARCHAR2(64),
  created_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL
);

-- テナント越境調査(あるテナントへのアクセス試行を時系列で追う)用。
CREATE INDEX idx_broker_audit_tenant ON platform_broker_audit(tenant, created_at);
-- 拒否(越境/スコープ不足)の抽出用。
CREATE INDEX idx_broker_audit_decision ON platform_broker_audit(decision, created_at);
