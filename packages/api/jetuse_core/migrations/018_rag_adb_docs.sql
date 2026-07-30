CREATE TABLE rag_adb_docs (
  owner_sub VARCHAR2(255) NOT NULL,
  doc_file VARCHAR2(400) NOT NULL,
  doc_version VARCHAR2(32) DEFAULT '0.0' NOT NULL,
  updated_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT rag_adb_docs_pk PRIMARY KEY (owner_sub, doc_file)
)
