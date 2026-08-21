-- VID-01 場面と修正履歴(specs/20 §1)。場面の中身を埋めるのは後続タスク(分析)で、
-- ここでは器だけを作る。
--
-- **NULL と 'unknown' を区別する**(specs/20 §1)。CHECK 制約は NULL を通すので、
-- indoor / time_of_day は「未分析 = NULL」「分析したが判らなかった = 'unknown'」を
-- どちらも表せる。既定値は入れない —— 既定で 'unknown' を埋めると未分析と区別できなくなる。
CREATE TABLE video_scenes (
  id VARCHAR2(64) PRIMARY KEY,
  asset_id VARCHAR2(64) NOT NULL,
  start_ms NUMBER NOT NULL,
  end_ms NUMBER NOT NULL,
  description CLOB,
  tags CLOB,
  objects CLOB,
  people CLOB,
  place VARCHAR2(255 CHAR),
  scene_kind VARCHAR2(64 CHAR),
  indoor VARCHAR2(16),
  time_of_day VARCHAR2(16),
  weather VARCHAR2(64 CHAR),
  actions CLOB,
  screen_text CLOB,
  thumb_object VARCHAR2(1024),
  source VARCHAR2(16) DEFAULT 'ai' NOT NULL,
  confirmed_at TIMESTAMP,
  embedding VECTOR(1024, FLOAT32),
  CONSTRAINT video_scenes_asset_fk FOREIGN KEY (asset_id)
    REFERENCES video_assets(id) ON DELETE CASCADE,
  CONSTRAINT video_scenes_range_ck CHECK (end_ms >= start_ms),
  CONSTRAINT video_scenes_source_ck CHECK (source IN ('ai', 'human', 'ai_confirmed')),
  CONSTRAINT video_scenes_indoor_ck CHECK (indoor IN ('indoor', 'outdoor', 'unknown')),
  CONSTRAINT video_scenes_tod_ck CHECK (time_of_day IN ('day', 'night', 'unknown')),
  CONSTRAINT video_scenes_tags_ck CHECK (tags IS JSON),
  CONSTRAINT video_scenes_objects_ck CHECK (objects IS JSON),
  CONSTRAINT video_scenes_people_ck CHECK (people IS JSON),
  CONSTRAINT video_scenes_actions_ck CHECK (actions IS JSON)
);

-- 詳細・タイムラインは映像ごとに時刻順(jetuse_core.video.get_asset)。
CREATE INDEX video_scenes_asset_idx ON video_scenes(asset_id, start_ms);

-- 人が何を直したかの履歴(specs/20 §1 / 要求8)。
-- 列名が before / after でないのは Oracle の予約語まわりを避けるため(意味は仕様のまま)。
-- 場面が消えれば履歴の指す先も消えるので ON DELETE CASCADE。
CREATE TABLE video_scene_edits (
  id VARCHAR2(64) PRIMARY KEY,
  scene_id VARCHAR2(64) NOT NULL,
  field VARCHAR2(64 CHAR) NOT NULL,
  before_value CLOB,
  after_value CLOB,
  edited_by VARCHAR2(255) NOT NULL,
  edited_at TIMESTAMP DEFAULT SYS_EXTRACT_UTC(SYSTIMESTAMP) NOT NULL,
  CONSTRAINT video_scene_edits_scene_fk FOREIGN KEY (scene_id)
    REFERENCES video_scenes(id) ON DELETE CASCADE
);

CREATE INDEX video_scene_edits_scene_idx ON video_scene_edits(scene_id, edited_at)
