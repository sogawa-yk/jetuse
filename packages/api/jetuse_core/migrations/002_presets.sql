CREATE TABLE prompt_presets (
  id VARCHAR2(36) PRIMARY KEY,
  owner_sub VARCHAR2(255) NOT NULL,
  name VARCHAR2(200) NOT NULL,
  content CLOB NOT NULL,
  created_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL
);

CREATE INDEX idx_preset_owner ON prompt_presets(owner_sub, created_at)
