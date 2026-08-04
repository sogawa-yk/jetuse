-- 会話 → demo 紐付け(specs/17 §5)。利用は SP2-03 だが、スキーマ変更は SP2-01 の migration 群に集約。
-- FK は張らない(削除後始末は specs/18 §3.2 の順序制御 — ON DELETE CASCADE で外部リソースと乖離させない)。
ALTER TABLE conversations ADD demo_id VARCHAR2(36)
