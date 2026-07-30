CREATE TABLE rag_adb_ingest (
  owner_sub VARCHAR2(255) NOT NULL,
  file_id VARCHAR2(36) NOT NULL,
  doc_key VARCHAR2(400) NOT NULL,
  status VARCHAR2(20) DEFAULT 'pending' NOT NULL,
  chunks NUMBER(9) DEFAULT 0 NOT NULL,
  error VARCHAR2(1000),
  updated_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT rag_adb_ingest_pk PRIMARY KEY (owner_sub, file_id)
)
