-- connector(L2 MCP)の登録先(CON-01)。kind: connector の定義をインスタンスへ登録して記録する。
-- 出所追跡(ADR-0013 D6): plugin_id / source_version は manifest 由来(installed_plugins と対応)。
-- 幅は manifest の MAX_ID_LEN(255) / MAX_VERSION_LEN(64) と一致させる(検証済み定義が必ず保存できる)。
-- definition CLOB には contributes["connector"](provider/transport/actions/auth)を配布表現のまま格納する。
-- **実シークレット値(トークン/パスワード)は保存しない**。definition に含まれる secret_ref は実値ではなく
-- 宣言の一部である論理参照名(非機密。spec §12.2)であり保持してよい。実シークレットは install 時に
-- Vault(OCID)へ束ねる(CON-02/03)。本テーブルは秘密値の列を持たない。
-- name/registered_by は多バイト文字(日本語名・非ASCII)を含みうるため CHAR セマンティクスで桁数を
-- 文字数で確保する(BYTE 既定の環境でも len()ベースの上限検証[connector_store.py]と一致し ORA-12899 を防ぐ)。
-- plugin_id/source_version/provider/transport は規則上 ASCII のみなので幅は BYTE/CHAR 同値。
CREATE TABLE connector_instances (
  id VARCHAR2(36) PRIMARY KEY,
  plugin_id VARCHAR2(255) NOT NULL,
  source_version VARCHAR2(64) NOT NULL,
  name VARCHAR2(200 CHAR) NOT NULL,
  provider VARCHAR2(64) NOT NULL,
  transport VARCHAR2(16) NOT NULL,
  definition CLOB NOT NULL,
  registered_by VARCHAR2(255 CHAR) NOT NULL,
  created_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL
);

CREATE INDEX idx_connector_plugin ON connector_instances(plugin_id, source_version);

CREATE INDEX idx_connector_provider ON connector_instances(provider);
