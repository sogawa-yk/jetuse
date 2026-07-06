-- 一覧 = owner + updated_at DESC(usecases の索引パターン踏襲 — specs/18 §1.1)
CREATE INDEX idx_demos_owner ON demos(owner_sub, updated_at)
