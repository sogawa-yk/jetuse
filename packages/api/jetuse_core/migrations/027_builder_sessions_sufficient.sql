ALTER TABLE builder_sessions ADD (
  sufficient NUMBER(1) DEFAULT 0 NOT NULL
    CONSTRAINT ck_bs_sufficient CHECK (sufficient IN (0,1))
)
