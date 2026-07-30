CREATE TABLE rag_adb_chunks (
  chunk_id VARCHAR2(128) PRIMARY KEY,
  owner_sub VARCHAR2(255) NOT NULL,
  file_id VARCHAR2(36) NOT NULL,
  chunk_no NUMBER(6) DEFAULT 0 NOT NULL,
  doc_file VARCHAR2(400) NOT NULL,
  doc_version VARCHAR2(32) DEFAULT '1.0' NOT NULL,
  sheet_name VARCHAR2(128),
  cells VARCHAR2(64),
  sha256 VARCHAR2(64) NOT NULL,
  kind VARCHAR2(32) DEFAULT 'doc' NOT NULL,
  current_version CHAR(1) DEFAULT 'Y' NOT NULL,
  attributes JSON,
  body CLOB NOT NULL,
  embedding VECTOR(1024, FLOAT32),
  created_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT rag_adb_chunks_cv_ck CHECK (current_version IN ('Y', 'N'))
)
