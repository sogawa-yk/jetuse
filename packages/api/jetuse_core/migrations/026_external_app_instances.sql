-- external-app（kind: external-app / ASSET-01 / BE-06）の登録先。kind: external-app の定義を
-- インスタンスへ登録して記録する。これにより external-app をマーケット install（installer / MKT-01）で
-- オンボードできる（specs/16-platform.md §14.4 で後段としていた store＋migration を本タスクで実装）。
-- 出所追跡（ADR-0013 D6）: plugin_id / source_version は manifest 由来（installed_plugins と対応）。
-- 幅は manifest の MAX_ID_LEN(255) / MAX_VERSION_LEN(64) と一致させる（検証済み定義が必ず保存できる）。
-- definition CLOB には contributes["external-app"]（embed/url/title/sso）を配布表現のまま格納する。
-- **実シークレット値（client_secret / id_token）は保存しない**。definition に含まれる clientIdRef /
-- secretRef は実値ではなく宣言の一部である論理参照名（非機密。spec §14.2 / §12.2）であり保持してよい。
-- 実シークレットは install 時に Vault(OCID) へ束ねる（人間ゲート）。本テーブルは秘密値の列を持たない。
-- name/registered_by は多バイト文字（日本語名・非ASCII）を含みうるため CHAR セマンティクスで確保する
-- （BYTE 既定の環境でも len()ベースの上限検証[external_app_store.py]と一致し ORA-12899 を防ぐ）。
-- plugin_id/source_version/app/embed は規則上 ASCII のみなので幅は BYTE/CHAR 同値。
CREATE TABLE external_app_instances (
  id VARCHAR2(36) PRIMARY KEY,
  plugin_id VARCHAR2(255) NOT NULL,
  source_version VARCHAR2(64) NOT NULL,
  name VARCHAR2(200 CHAR) NOT NULL,
  app VARCHAR2(64) NOT NULL,
  embed VARCHAR2(16) NOT NULL,
  definition CLOB NOT NULL,
  registered_by VARCHAR2(255 CHAR) NOT NULL,
  created_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL
);

CREATE INDEX idx_external_app_plugin ON external_app_instances(plugin_id, source_version);

CREATE INDEX idx_external_app_app ON external_app_instances(app);
