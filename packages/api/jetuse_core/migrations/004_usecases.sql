CREATE TABLE usecases (
  id VARCHAR2(36) PRIMARY KEY,
  owner_sub VARCHAR2(255) NOT NULL,
  name VARCHAR2(200) NOT NULL,
  description VARCHAR2(1000),
  icon VARCHAR2(16),
  tags VARCHAR2(400),
  model VARCHAR2(64),
  definition CLOB NOT NULL,
  visibility VARCHAR2(10) DEFAULT 'private' NOT NULL,
  created_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
  updated_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL
);

CREATE INDEX idx_usecase_owner ON usecases(owner_sub, updated_at);

CREATE INDEX idx_usecase_visibility ON usecases(visibility)
