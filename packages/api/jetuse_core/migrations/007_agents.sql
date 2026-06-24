CREATE TABLE agents (
  id VARCHAR2(36) PRIMARY KEY,
  owner_sub VARCHAR2(255) NOT NULL,
  name VARCHAR2(200) NOT NULL,
  description VARCHAR2(1000),
  icon VARCHAR2(16),
  instructions CLOB NOT NULL,
  model VARCHAR2(64) NOT NULL,
  enabled_tools VARCHAR2(1000),
  mcp_server_ids VARCHAR2(1000),
  project_ocid VARCHAR2(255),
  visibility VARCHAR2(10) DEFAULT 'private' NOT NULL,
  tags VARCHAR2(400),
  created_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
  updated_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL
);

CREATE INDEX idx_agents_owner ON agents(owner_sub, updated_at);

CREATE INDEX idx_agents_visibility ON agents(visibility)
