CREATE TABLE demo_backend_targets (
  id VARCHAR2(36) PRIMARY KEY,
  namespace VARCHAR2(255) NOT NULL,
  kind VARCHAR2(20) NOT NULL
    CONSTRAINT ck_dbt_kind CHECK (kind IN ('vector_store','files','select_ai','opensearch','objectstorage')),
  locator CLOB NOT NULL CONSTRAINT ck_dbt_locator CHECK (locator IS JSON),
  locator_hash VARCHAR2(64) NOT NULL,
  created_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT uq_dbt UNIQUE (namespace, kind, locator_hash)
);
