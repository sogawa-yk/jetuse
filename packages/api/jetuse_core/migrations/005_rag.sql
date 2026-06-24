CREATE TABLE rag_stores (
  owner_sub VARCHAR2(255) PRIMARY KEY,
  vector_store_id VARCHAR2(128) NOT NULL,
  created_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL
);

CREATE TABLE rag_files (
  id VARCHAR2(36) PRIMARY KEY,
  owner_sub VARCHAR2(255) NOT NULL,
  filename VARCHAR2(400) NOT NULL,
  oci_file_id VARCHAR2(128) NOT NULL,
  status VARCHAR2(20) DEFAULT 'processing' NOT NULL,
  bytes NUMBER,
  error VARCHAR2(1000),
  created_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL
);

CREATE INDEX idx_rag_files_owner ON rag_files(owner_sub, created_at)
