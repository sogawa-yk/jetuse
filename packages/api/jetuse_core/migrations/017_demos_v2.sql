-- Demo エンティティ完全形(SP2-01 / specs/18 §1.1)。1 ファイル = 1 文(再実行耐性 — §1.1)。
-- 列追加は demos への 1 つの ADD (...) に束ねて原子化。config は不透明 JSON(IS JSON のみ)、
-- status の値域は §1.2 の状態機械、updated_at は全 UPDATE 文で SYSTIMESTAMP(トリガ不使用)。
ALTER TABLE demos ADD (description VARCHAR2(1000 CHAR), config CLOB DEFAULT '{}' NOT NULL CHECK (config IS JSON), status VARCHAR2(20) DEFAULT 'ready' NOT NULL CHECK (status IN ('provisioning','ready','failed','deleting')), updated_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL)
