CREATE TABLE minutes_jobs (
  id VARCHAR2(36) PRIMARY KEY,
  owner_sub VARCHAR2(255) NOT NULL,
  title VARCHAR2(400) NOT NULL,
  status VARCHAR2(20) DEFAULT 'processing' NOT NULL,
  language VARCHAR2(10) DEFAULT 'ja' NOT NULL,
  audio_object VARCHAR2(700) NOT NULL,
  oci_job_id VARCHAR2(128),
  duration_sec NUMBER,
  speaker_count NUMBER,
  transcript CLOB,
  error VARCHAR2(1000),
  created_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL
);

CREATE INDEX idx_minutes_owner ON minutes_jobs(owner_sub, created_at)
