CREATE TABLE builder_sessions (
  id          VARCHAR2(36) PRIMARY KEY,
  owner_sub   VARCHAR2(255) NOT NULL,
  status      VARCHAR2(20) DEFAULT 'hearing' NOT NULL
              CONSTRAINT ck_bs_status CHECK (status IN ('hearing','designed')),
  transcript  CLOB DEFAULT '[]' NOT NULL CONSTRAINT ck_bs_transcript CHECK (transcript IS JSON),
  requirements CLOB CONSTRAINT ck_bs_requirements CHECK (requirements IS JSON),
  plan        CLOB CONSTRAINT ck_bs_plan CHECK (plan IS JSON),
  demo_id     VARCHAR2(36),
  created_at  TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
  updated_at  TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL
);
