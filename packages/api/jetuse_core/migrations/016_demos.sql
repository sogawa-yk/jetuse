-- demos 最小レジストリ(SP1-02 / specs/17 §5)。所有権検証に必要な最小列のみ。
-- config・箱のプロビジョニング状態などの列は SP2 で追加する(specs/17 §8 スコープ境界)。
CREATE TABLE demos (
  id VARCHAR2(36) PRIMARY KEY,
  owner_sub VARCHAR2(255) NOT NULL,
  name VARCHAR2(200 CHAR) NOT NULL,
  visibility VARCHAR2(10) DEFAULT 'private' NOT NULL,
  created_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL
)
