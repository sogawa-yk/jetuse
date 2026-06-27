# タスク: PAPI-03 実 Platform API ルート（rag.search / db.query / connector.invoke）

## ゴール
PAPI-01 の認可コア（`jetuse_core/platform_broker.py`：発行/検証/スコープ強制/テナント境界/監査）と
PAPI-02 の承認＋発行フロー（`jetuse_core/platform_grants.py`）の上に、**実 Platform API ルート**
（`/platform/*`）を実装する。各エンドポイントは冒頭で **broker トークンを JWT 検証 → scope チェック →
テナント一致** を強制し（＝`platform_broker.authorize`）、通過した範囲でだけ**既存エンジンへ委譲**する。
全アクセス（ALLOW/DENY）は `platform_broker_audit` に記録する。`db.query` は**読取限定**、`scope` 不足は
**403**、テナント越境は**拒否（403）**。`connector.invoke` は**配管まで**（実 MCP 呼び出しは CON-02/03）。

## 対象 area
api ＋ docs

## 依存
PAPI-01 / PAPI-02 / CON-01 完了（base=feat/stage-3。`platform_broker.py` / `platform_grants.py` /
`plugins/connector.py` / migration 020・021 済）。後続: rag.search の本格ベクトル検索・OIDC 発行主体認証
（INFRA-02）・レート制限。

## 仕様参照
docs/decisions/ADR-0014（採用済。§3 テナント境界・§4 監査・§5 fail-closed）/ specs/16-platform.md §13
（§13.5「実 API ルートは各エンドポイントの冒頭で `authorize(token, required_scope, tenant=...)` を呼ぶ」）/
docs/enhance/202607-demo-platform-plan.md §7・§269。

## 受け入れ条件（検証可能な述語で書く）
- [ ] `service/routes/platform.py` が `/platform/*` ルート（最低 `db.query` / `rag.search` /
      `connector.invoke`）を提供し、`create_app()` に登録される。route の冒頭で broker トークン
      （`Authorization: Bearer <broker-jwt>`）を `platform_broker.authorize(token, required_scope, tenant=...)`
      に通す（JWT 検証 → scope 強制 → テナント一致 → 監査）。OIDC ユーザトークン（`require_user`）とは別系統。
- [ ] `POST /platform/db/query`（scope `platform:db.query`）は **読取限定**で既存エンジン
      `nl2sql.execute_readonly` へ委譲する。非 SELECT（INSERT/UPDATE/DDL 等）は `SqlRejectedError` 経由で
      **400** に倒す（書込は到達しない）。正常系は行を返す。
- [ ] **scope 不足は 403**：トークンに必要 scope が無いエンドポイント要求は `scope_denied` → **403**。
      改竄/期限切れ/未署名トークンは `invalid_token` → **401**。ブローカー鍵未設定は **503**。
- [ ] **テナント越境拒否**：トークンの `tenant` と要求リソースのテナント（リクエスト指定）が不一致なら
      `tenant_mismatch` → **403**。scope 不足より先にテナント不一致を監査に残す（PAPI-01 の契約に従う）。
- [ ] **全アクセス監査**：ALLOW/DENY のいずれも `platform_broker_audit` に記録される（`authorize` 経由。
      越境試行 DENY が必ず残る）。
- [ ] `POST /platform/connector/invoke`（scope `platform:connector.invoke`）は**配管まで**：authorize を
      通し、インストール済みコネクタ／action の存在検証までを行い、実 MCP 呼び出しは未実装として
      **501**（理由＝CON-02/03）を返す。authorize（検証・監査）は本実装と同一。
- [ ] `POST /platform/rag/search`（scope `platform:rag.search`）は authorize を通す配管を提供する
      （本格ベクトル検索＝OCI Responses 委譲は後続。authorize 通過後 **501** 理由付き）。
- [ ] `.venv/bin/pytest packages/api/tests` 全件パス（authorize 通過/各拒否系＝scope/tenant/invalid・
      db.query 読取限定・委譲・connector.invoke 配管を網羅）／`.venv/bin/ruff check packages/api` クリーン。
      既存の公開シグネチャ・ルート path/method を壊さない。
- [ ] specs/16-platform.md §13 に実ルート節（`/platform/*` の表＝path/scope/委譲先/拒否マッピング）を追記。

## E2E シナリオ（実環境 / jetuse-dev・固定 loop 環境・最低2本・専用スキーマ JETUSE_PAPI-03 隔離）
完了ゲートで Claude が jetuse-dev の固定 loop ADB へ専用スキーマ `JETUSE_PAPI-03` で migration を適用し、
spike スクリプト（`spikes/spike07_platform_api.py`）を実 ADB に対して実行（FastAPI TestClient で実ルートを
叩く）して証跡を `runs/<run-id>/e2e/` に残す。
- [ ] シナリオ1（db.query 正常系）: manifest（permissions=[rag.search, db.query]）に対し tenant T・plugin P
      へ **db.query を承認** → `issue_token` で broker トークン発行 → `POST /platform/db/query`（Bearer）で
      JETUSE_PAPI-03 スキーマの実テーブルへ **SELECT** → **200＋行**が返る → `platform_broker_audit` に
      **ALLOW 行**（実 ADB から SELECT して証跡化）。
- [ ] シナリオ2（拒否系）: (a) `db.query` 未承認トークンで `/platform/connector/invoke` 要求 →
      `scope_denied` で **403**、(b) token tenant=T だが要求 tenant=T2 → `tenant_mismatch` で **403**、
      (c) 改竄トークン → `invalid_token` で **401**、(d) `db.query` に INSERT/UPDATE を投げる → **400**
      （読取限定）。各 DENY が `platform_broker_audit` に残ることを SELECT で証跡化。
- [ ] 実施不能な範囲（rag.search の本格ベクトル検索＝OCI Responses 委譲、connector.invoke の実 MCP 呼び出し
      ＝CON-02/03、OIDC 発行主体認証＝INFRA-02、レート制限）は `runs/<run-id>/e2e/SKIPPED.md` に理由明記。

## 非ゴール / 制約
- rag.search の本格ベクトル検索（OCI Responses file_search 委譲）・connector.invoke の実 MCP 呼び出しは
  後続（CON-02/03）。本タスクは authorize 配管＋db.query 読取委譲に集中する。
- テナント→物理スキーマの本格ルーティング（per-tenant 隔離の DB 実体）は INFRA 範囲。本タスクは
  broker のテナント境界（token.tenant == 要求 tenant）の強制までを担う。
- 認証情報・テナンシ/コンパートメント OCID・**ブローカー署名鍵をコミットしない**
  （`platform_broker_secret` は .env / Vault 注入）。
- spec-driven: 仕様にない判断は実装せず ADR に書く。コミット/PR/push は人間承認後。
