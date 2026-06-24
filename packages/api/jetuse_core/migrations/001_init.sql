CREATE TABLE conversations (
  id VARCHAR2(36) PRIMARY KEY,
  owner_sub VARCHAR2(255) NOT NULL,
  title VARCHAR2(400),
  model VARCHAR2(64),
  created_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
  updated_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL
);

CREATE INDEX idx_conv_owner ON conversations(owner_sub, updated_at);

CREATE TABLE messages (
  id VARCHAR2(36) PRIMARY KEY,
  conversation_id VARCHAR2(36) NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  seq NUMBER NOT NULL,
  role VARCHAR2(16) NOT NULL,
  content CLOB NOT NULL,
  created_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL
);

CREATE INDEX idx_msg_conv ON messages(conversation_id, seq);

CREATE TABLE usage_log (
  id VARCHAR2(36) PRIMARY KEY,
  owner_sub VARCHAR2(255) NOT NULL,
  conversation_id VARCHAR2(36),
  model VARCHAR2(64),
  input_tokens NUMBER,
  output_tokens NUMBER,
  created_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL
);
