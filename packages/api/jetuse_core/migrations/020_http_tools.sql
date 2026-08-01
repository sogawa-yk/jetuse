CREATE TABLE http_tools (
  id VARCHAR2(36) PRIMARY KEY,
  owner_sub VARCHAR2(255) NOT NULL,
  name VARCHAR2(64) NOT NULL,
  description VARCHAR2(1000) NOT NULL,
  parameters CLOB NOT NULL,
  url VARCHAR2(1000) NOT NULL,
  method VARCHAR2(8) DEFAULT 'GET' NOT NULL,
  auth_header VARCHAR2(64),
  auth_secret_ocid VARCHAR2(255),
  created_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT http_tools_owner_name UNIQUE (owner_sub, name)
);

CREATE INDEX idx_http_tools_owner ON http_tools(owner_sub, created_at)
