-- MKT-02: 中央レジストリ μService の ADB バックエンド。
-- PLG-04 の Object Storage + index.json を ADB へ昇格する。版/発行者公開鍵/評価/DL 数/版ライフサイクルを
-- ADB に保持し、検索を SQL で行う。成果物(manifest 全文)は同じ行の CLOB に持ち μService を ADB 自己完結に
-- する(E2E は loop ADB のみで成立し Object Storage を要さない)。署名検証(ed25519)は service 層が維持。
-- 注意: migrate ランナーはセミコロン終端の単文のみ(PL/SQL ブロック非対応)。トリガは使わずアプリ側で原子性を担保。

-- 発行者公開鍵(publish の署名検証で使う ed25519 公開鍵)。(publisher, public_key_id) は不変。
CREATE TABLE registry_publisher_keys (
  publisher VARCHAR2(255) NOT NULL,
  public_key_id VARCHAR2(255) NOT NULL,
  public_key VARCHAR2(512) NOT NULL,
  registered_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT pk_registry_pub_keys PRIMARY KEY (publisher, public_key_id)
);

-- プラグイン版エントリ。(plugin_id, version) を PK にして版の不変性(再 publish 拒否)を DB 制約で担保する。
-- manifest は成果物(正準 JSON)全文を CLOB に保持(content-addressed。sha256 で完全性検証)。
CREATE TABLE registry_plugins (
  plugin_id VARCHAR2(255) NOT NULL,
  version VARCHAR2(64) NOT NULL,
  kind VARCHAR2(32) NOT NULL,
  name VARCHAR2(1000) NOT NULL,
  description VARCHAR2(4000) DEFAULT '' NOT NULL,
  publisher VARCHAR2(255) NOT NULL,
  tags VARCHAR2(4000) DEFAULT '[]' NOT NULL,
  object_path VARCHAR2(1000) NOT NULL,
  sha256 VARCHAR2(64) NOT NULL,
  public_key_id VARCHAR2(255) NOT NULL,
  published_at VARCHAR2(40) NOT NULL,
  lifecycle VARCHAR2(16) DEFAULT 'active' NOT NULL
    CHECK (lifecycle IN ('active', 'deprecated', 'yanked')),
  download_count NUMBER DEFAULT 0 NOT NULL,
  manifest CLOB NOT NULL,
  CONSTRAINT pk_registry_plugins PRIMARY KEY (plugin_id, version)
);

CREATE INDEX idx_registry_plugins_id ON registry_plugins(plugin_id);

CREATE INDEX idx_registry_plugins_kind ON registry_plugins(kind, lifecycle);

-- 評価(プラグイン id 単位)。1 rater 1 件(upsert)。score は 1〜5。
CREATE TABLE registry_ratings (
  plugin_id VARCHAR2(255) NOT NULL,
  rater VARCHAR2(255) NOT NULL,
  score NUMBER(1) NOT NULL CHECK (score BETWEEN 1 AND 5),
  -- COMMENT は Oracle 予約語のため列名は comment_text。Oracle は空文字を NULL 扱いするため
  -- NOT NULL を付けず nullable とする(コメント未指定=NULL。アプリ側で None→"" に正規化)。
  comment_text VARCHAR2(2000),
  created_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT pk_registry_ratings PRIMARY KEY (plugin_id, rater)
);

CREATE INDEX idx_registry_ratings_id ON registry_ratings(plugin_id);
