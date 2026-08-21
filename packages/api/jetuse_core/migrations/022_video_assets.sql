-- VID-01 映像本体の台帳(specs/20 §1)。映像そのものは Object Storage に置き、
-- ここには位置(object_name)だけを持つ。
--
-- **NULL と 'unknown' を混ぜない**(specs/20 §1 / ADR-0032 決定5)。
-- captured_at・summary・duration_ms は「まだ分からない/未分析」を NULL で表す。
-- 時刻は **UTC で入れる**(既定値も SYS_EXTRACT_UTC)。TIMESTAMP はタイムゾーンを持たないので、
-- 入れる側で寄せておかないと DB の時間帯設定次第で期間検索が静かにずれる。
-- vision_state の NULL は「AI Vision 層に触れていない」、'skipped' は
-- 「触れたが使えないので縮退した」。この 2 つを同じ値にすると縮退したことが残らない。
CREATE TABLE video_assets (
  id VARCHAR2(64) PRIMARY KEY,
  owner_sub VARCHAR2(255) NOT NULL,
  title VARCHAR2(500 CHAR) NOT NULL,
  summary CLOB,
  captured_at TIMESTAMP,
  created_at TIMESTAMP DEFAULT SYS_EXTRACT_UTC(SYSTIMESTAMP) NOT NULL,
  duration_ms NUMBER,
  collection VARCHAR2(255 CHAR),
  category VARCHAR2(255 CHAR),
  rights VARCHAR2(1000 CHAR),
  object_name VARCHAR2(1024) NOT NULL,
  thumb_object VARCHAR2(1024),
  analysis_state VARCHAR2(32) DEFAULT 'pending' NOT NULL,
  analysis_error VARCHAR2(4000),
  vision_state VARCHAR2(32),
  CONSTRAINT video_assets_state_ck
    CHECK (analysis_state IN ('pending', 'running', 'done', 'failed', 'partial')),
  CONSTRAINT video_assets_vision_ck
    CHECK (vision_state IN ('pending', 'running', 'done', 'failed', 'skipped'))
);

-- 一覧は所有者ごと・新しい順(jetuse_core.video.list_assets)。
CREATE INDEX video_assets_owner_idx ON video_assets(owner_sub, created_at)
