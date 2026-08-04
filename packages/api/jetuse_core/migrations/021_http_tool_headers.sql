ALTER TABLE http_tools ADD (
  extra_headers CLOB,
  idempotency_header VARCHAR2(64)
)
