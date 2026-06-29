-- Platform テナント RAG ストア登録簿(BE-04)。
-- テナント(Project OCID)→ そのテナントが所有するベクトルストア(OCI Responses file_search 対象)の
-- 解決元。/api/platform/rag/search はこの登録簿だけを正本にストアを解決する。呼び出し元(プラグイン)
-- は store id を渡さない/受け取らないため、別テナントのストアへは構造的に到達できない。
--
-- 既存 rag_stores はユーザ(owner_sub=OIDC sub)単位であり、テナント(Project OCID)単位ではない。
-- Platform 経路はテナント境界(ADR-0014)を強制するため、ユーザ単位の rag_stores とは別系統の
-- テナント単位登録簿を持つ(キーの取り違えを避ける)。登録(取込側からの upsert)は
-- jetuse_core.rag.register_tenant_store が担う。
--
-- vector_store_id は **UNIQUE**: 1 つの実在ストアは高々 1 テナントにしか属さない。これにより
-- 「別テナントが同一ストアを登録して両者の正規トークンから同一文書へ到達する」越境(BE04-001)を
-- DB 制約で構造的に封じる(OCI 側 Project 分離が厳密でないテナンシでも一次境界を保証する)。
-- 別テナントへの既登録ストアの再登録は一意制約違反 → register_tenant_store が StoreConflictError→409。
CREATE TABLE platform_rag_stores (
  tenant VARCHAR2(255) PRIMARY KEY,
  vector_store_id VARCHAR2(128) NOT NULL,
  created_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
  updated_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT uq_platform_rag_stores_vs UNIQUE (vector_store_id)
);
