CREATE TABLE installed_plugins (
  id VARCHAR2(36) PRIMARY KEY,
  plugin_id VARCHAR2(255) NOT NULL,
  version VARCHAR2(64) NOT NULL,
  kind VARCHAR2(20) NOT NULL,
  source_registry VARCHAR2(255),
  manifest CLOB NOT NULL,
  signature_verified NUMBER(1) DEFAULT 0 NOT NULL
    CHECK (signature_verified IN (0, 1)),
  installed_by VARCHAR2(255) NOT NULL,
  installed_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL
);

CREATE UNIQUE INDEX uq_installed_plugin_ver ON installed_plugins(plugin_id, version);

CREATE INDEX idx_installed_plugin_kind ON installed_plugins(kind, installed_at);
