-- デモ起動記録(HBD-05)。一気通貫(ヒアリング→合成→バリデーションPASS→起動)で、
-- バリデーションを通過したデモ構成を「起動」した事実を永続する。出典: docs/enhance/
-- 202607-demo-platform-plan.md §10「HBD-05」/ 202607-hearing-flow.md §5。
-- 本タスクの「起動」は既存 loop 基盤上のデモ(コンテナ配備 L3 は S4)。よってここでは
-- 起動済みデモを当該セッションの解決済みインスタンス(コア同梱 SBA)＋構成スナップショットへ
-- 結びつけて記録し、起動デモの再表示・主役 AI 機能の実行導線(/sba/{instance_id})に使う。
-- 冪等性: migrate.py が schema_migrations に version を記録し再適用はスキップする(018 の冪等再適用)。
-- 単文(セミコロン終端)のみ。コメント内にセミコロンを置かない。文字幅は CHAR セマンティクスで確保。

-- 1 セッション 1 起動(最新で差し替え)。governance PASS を通った構成だけが書かれる。
CREATE TABLE demo_launch (
  id VARCHAR2(36) PRIMARY KEY,
  session_id VARCHAR2(36) NOT NULL
    REFERENCES hearing_session(id) ON DELETE CASCADE,
  owner_sub VARCHAR2(255 CHAR) NOT NULL,
  sample_app VARCHAR2(16) NOT NULL,
  instance_id VARCHAR2(64) NOT NULL,
  entry_slot VARCHAR2(64),
  demo_url VARCHAR2(256) NOT NULL,
  composition CLOB NOT NULL,
  status VARCHAR2(32) DEFAULT 'launched' NOT NULL,
  launched_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT uq_demo_launch_session UNIQUE (session_id)
);

CREATE INDEX idx_demo_launch_owner ON demo_launch(owner_sub, launched_at);
