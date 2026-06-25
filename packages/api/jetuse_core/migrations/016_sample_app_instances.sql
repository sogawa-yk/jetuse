-- sample-app の scaffold 展開先(SBA-01)。kind: sample-app の定義をインスタンスへ展開して記録する。
-- 出所追跡(ADR-0013 D6): plugin_id / source_version は manifest 由来(installed_plugins と対応)。
-- 幅は manifest の MAX_ID_LEN(255) / MAX_VERSION_LEN(64) と一致させる(検証済み定義が必ず保存できる)。
-- definition CLOB には contributes["sample-app"](screens/datasets/aiSlots)を配布表現のまま格納する。
-- name/created_by は多バイト文字(日本語名・非ASCII の created_by)を含みうるため CHAR セマンティクスで
-- 桁数を文字数で確保する(BYTE 既定の環境でも len()ベースの上限検証[scaffold.py]と一致し ORA-12899 を防ぐ)。
-- plugin_id/source_version は manifest の id/version 規則上 ASCII のみなので幅は BYTE/CHAR 同値。
CREATE TABLE sample_app_instances (
  id VARCHAR2(36) PRIMARY KEY,
  plugin_id VARCHAR2(255) NOT NULL,
  source_version VARCHAR2(64) NOT NULL,
  name VARCHAR2(200 CHAR) NOT NULL,
  definition CLOB NOT NULL,
  created_by VARCHAR2(255 CHAR) NOT NULL,
  created_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL
);

CREATE INDEX idx_sample_app_plugin ON sample_app_instances(plugin_id, source_version);

-- データシードの展開先。1 行 = sample-app dataset の 1 シード行(payload は JSON)。
-- instance 削除時に連動削除する(ON DELETE CASCADE)。
CREATE TABLE sample_app_seed_rows (
  id VARCHAR2(36) PRIMARY KEY,
  instance_id VARCHAR2(36) NOT NULL
    REFERENCES sample_app_instances(id) ON DELETE CASCADE,
  dataset VARCHAR2(64) NOT NULL,
  row_index NUMBER(10) NOT NULL,
  payload CLOB NOT NULL
);

CREATE INDEX idx_sample_seed_instance ON sample_app_seed_rows(instance_id, dataset);
