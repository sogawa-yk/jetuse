-- ヒアリングフロー(HBD-01)。顧客ヒアリングの構造化保存＋推薦構成の永続。
-- 出典: docs/enhance/202607-hearing-flow.md §7 データモデル → specs/16-platform.md §11 へ昇格。
-- 冪等性: マイグレーションランナー(migrate.py)が schema_migrations に version を記録し、
--   適用済みは再実行でスキップする(= 017 の冪等再適用)。本ファイルは PL/SQL ブロック非対応の
--   単文(セミコロン終端)の並びに限る。コメント内にセミコロンを置かない(素朴な分割器が
--   文末と誤認するため)。
-- 文字幅: owner_sub / created_by 等は日本語・非ASCII を含みうるため CHAR セマンティクスで桁を確保
--   (BYTE 既定環境でも ORA-12899 を避ける)。question_id / status / source は ASCII 固定語彙。

-- セッション: 1 回のヒアリング。input_notes は GenAI 要点抽出の元になる自由記述(§3 入力ステップ)。
CREATE TABLE hearing_session (
  id VARCHAR2(36) PRIMARY KEY,
  owner_sub VARCHAR2(255 CHAR) NOT NULL,
  status VARCHAR2(32) DEFAULT 'draft' NOT NULL,
  input_notes CLOB,
  created_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
  updated_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL
);

CREATE INDEX idx_hearing_session_owner ON hearing_session(owner_sub, updated_at);

-- 回答: question_id ごとに 1 行。value は正規化済み回答(single=文字列 / multi=配列)の JSON。
-- source は §7: 'sa'(SA 手入力) | 'genai_suggested'(メモ要点抽出のデフォルト提案)。
-- (session_id, question_id) を一意にして再保存を upsert(差し替え)で扱う。
CREATE TABLE hearing_answer (
  id VARCHAR2(36) PRIMARY KEY,
  session_id VARCHAR2(36) NOT NULL
    REFERENCES hearing_session(id) ON DELETE CASCADE,
  question_id VARCHAR2(16) NOT NULL,
  value CLOB NOT NULL,
  source VARCHAR2(32) DEFAULT 'sa' NOT NULL,
  created_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
  updated_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT uq_hearing_answer UNIQUE (session_id, question_id)
);

-- 推薦構成: §7 のフィールド(sample_app / ai_parts / connectors / ui / seed_strategy / validation /
--   confirmed_at)＋監査トレース等の全文(detail JSON)。1 セッション 1 推薦(最新で差し替え)。
CREATE TABLE recommendation (
  id VARCHAR2(36) PRIMARY KEY,
  session_id VARCHAR2(36) NOT NULL
    REFERENCES hearing_session(id) ON DELETE CASCADE,
  sample_app VARCHAR2(16),
  ai_parts CLOB NOT NULL,
  connectors CLOB NOT NULL,
  ui VARCHAR2(32) NOT NULL,
  seed_strategy VARCHAR2(32) NOT NULL,
  validation CLOB NOT NULL,
  detail CLOB NOT NULL,
  confirmed_at TIMESTAMP,
  created_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT uq_recommendation_session UNIQUE (session_id)
);
