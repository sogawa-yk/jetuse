-- Platform API スコープ承認の永続化(PAPI-02 / ADR-0014 §2)。
-- (tenant=Project OCID, plugin_id) ごとに「インストール／合成時に人間=SA が承認したスコープ」を保持する。
-- 短期トークン発行フロー(platform_grants.issue_token)はこの承認に**厳密に閉じて**だけスコープを載せる。
-- 承認は (tenant, plugin_id) で一意 = upsert(再承認で更新)。失効は status=REVOKED(行は残し監査追跡可能に保つ)。
-- scopes はスペース区切りの承認スコープ集合(PLATFORM_SCOPES の部分集合 ⊆ manifest.permissions)。
--   語彙は ASCII(platform:* ＝最長 'platform:conversations.read'=27 文字×6+空白でも 200 未満)なので幅に余裕を持たせる。
-- source_version は承認時の manifest version(出所追跡。installed_plugins と対応。幅は manifest の MAX_VERSION_LEN=64)。
-- **トークン署名鍵(platform_broker_secret)・DB 認証情報・実シークレット値は一切保存しない**(ADR-0014 / CLAUDE.md)。
-- tenant/plugin_id/scopes/status は ASCII なので幅は BYTE/CHAR 同値。approved_by は多バイト(SA 名/メール)を
--   含みうるため CHAR セマンティクスで桁数を文字数で確保する(BYTE 既定環境でも len() ベースの上限検証と一致)。
CREATE TABLE platform_scope_grants (
  id VARCHAR2(36) PRIMARY KEY,
  tenant VARCHAR2(255) NOT NULL,
  plugin_id VARCHAR2(255) NOT NULL,
  source_version VARCHAR2(64) NOT NULL,
  scopes VARCHAR2(1000) NOT NULL,
  status VARCHAR2(16) DEFAULT 'ACTIVE' NOT NULL,
  approved_by VARCHAR2(255 CHAR) NOT NULL,
  created_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
  updated_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT uq_scope_grant_tenant_plugin UNIQUE (tenant, plugin_id),
  CONSTRAINT ck_scope_grant_status CHECK (status IN ('ACTIVE', 'REVOKED'))
);

-- あるテナントに承認済みのプラグイン一覧(承認状況の棚卸し)用。
CREATE INDEX idx_scope_grant_tenant ON platform_scope_grants(tenant, status);
-- あるプラグインがどのテナントへ承認されているか(出所→承認の追跡)用。
CREATE INDEX idx_scope_grant_plugin ON platform_scope_grants(plugin_id, source_version);
