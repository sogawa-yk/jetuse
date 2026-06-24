CREATE TABLE audit_log (
  id VARCHAR2(36) PRIMARY KEY,
  owner_sub VARCHAR2(255) NOT NULL,
  feature VARCHAR2(40) NOT NULL,
  model VARCHAR2(64),
  input_tokens NUMBER,
  output_tokens NUMBER,
  status VARCHAR2(20) DEFAULT 'ok' NOT NULL,
  meta VARCHAR2(1000),
  created_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL
);

CREATE INDEX idx_audit_owner ON audit_log(owner_sub, created_at);

CREATE INDEX idx_audit_created ON audit_log(created_at)
