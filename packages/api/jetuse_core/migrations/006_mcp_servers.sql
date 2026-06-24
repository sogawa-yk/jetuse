CREATE TABLE mcp_servers (
  id VARCHAR2(36) PRIMARY KEY,
  owner_sub VARCHAR2(255) NOT NULL,
  label VARCHAR2(100) NOT NULL,
  url VARCHAR2(1000) NOT NULL,
  auth_secret_ocid VARCHAR2(255),
  created_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL
);

CREATE INDEX idx_mcp_owner ON mcp_servers(owner_sub, created_at)
