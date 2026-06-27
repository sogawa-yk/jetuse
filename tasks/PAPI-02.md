# タスク: PAPI-02 スコープ承認＋短期トークン発行フロー

## ゴール
PAPI-01 で確定した認可コア（ADR-0014 / `jetuse_core/platform_broker.py`）の上に、**スコープ承認**
（manifest `permissions` から人間=SA が承認した範囲を永続化）と、その承認に**厳密に閉じた**
**短期トークン発行フロー**を実装する。トークンは「インストール／合成時に承認されたスコープ」だけを
載せ、承認を超えるスコープは決して発行しない（fail-closed）。**DB 認証情報はトークンに載せない**
（ADR-0014）。plan §7 を `specs/16-platform.md` へ昇格する。

## 対象 area
api ＋ docs

## 依存
PAPI-01 完了（feat/stage-3 ベース。`platform_broker.py` / migration 020 / settings 済）。
後続: PAPI-03（実 Platform API ルート rag.search/db.query 等の本体）。

## 仕様参照
docs/decisions/ADR-0014（採用済。§1 スコープ語彙・§2 短期トークン・**§2 末「発行粒度は PAPI-02 で確定」**）/
docs/enhance/202607-demo-platform-plan.md §7 / specs/16-platform.md §4・§7（manifest permissions）

## 受け入れ条件（検証可能な述語で書く）
- [ ] migration（`platform_scope_grants`）で (tenant=Project OCID, plugin_id) ごとの**承認済みスコープ**を
      永続化できる。再適用は冪等（既適用は no-op）。承認は upsert（再承認で更新）、失効は status=REVOKED。
- [ ] `jetuse_core/platform_grants.py` が **スコープ承認**（`approve_scopes`）を提供する。承認スコープは
      **manifest `permissions` の部分集合**（＝プラグインが宣言・要求した範囲）かつ `PLATFORM_SCOPES` の
      部分集合のみ受理する。manifest が要求していないスコープの承認、未知スコープ、空は拒否（fail-closed）。
- [ ] `issue_token`（発行フロー）が、承認済みグラントを読み、**承認スコープに閉じた**短期 JWT を
      `platform_broker.issue_broker_token` 経由で発行する。グラント無し（`no_grant`）・失効（`grant_revoked`）・
      承認超過スコープ要求（`scope_not_granted`）は**トークンを発行せず**拒否する（fail-closed）。
      発行粒度は**呼び出しごと**（ADR-0014 §2 の委任を確定。リプレイ露出窓を最小化）。
- [ ] 発行されたトークンは `platform_broker.verify_broker_token` で検証でき、`scope` claim が
      承認スコープと一致する（manifest が宣言しても**未承認のスコープは載らない**）。
- [ ] `get_grant` / `list_grants` / `revoke_grant` を提供する。`platform_broker_secret` / DB 認証情報は
      トークン・グラント行のいずれにも保存しない。既存の公開シグネチャを壊さない。
- [ ] `.venv/bin/pytest packages/api/tests` 全件パス（承認の正常系＋境界＋発行の正常系＋拒否系を網羅）／
      `.venv/bin/ruff check packages/api` クリーン。
- [ ] plan §7 を `specs/16-platform.md` の新節（Platform API ブローカー / スコープ承認・発行フロー）へ
      昇格し、発行粒度の確定（呼び出しごと）を記す。

## E2E シナリオ（実環境 / jetuse-dev・固定 loop 環境・最低2本・専用スキーマ隔離）
完了ゲートで Claude が jetuse-dev の固定 loop ADB へ専用スキーマで migration を適用し、spike スクリプト
（`spikes/spike06_platform_grants.py`）を実 ADB に対して実行して証跡を `runs/<run-id>/e2e/` に残す。
- [ ] シナリオ1（承認→発行 正常系）: manifest（permissions=[rag.search, db.query]）に対し tenant T・
      plugin P へ **rag.search のみ承認** → `platform_scope_grants` に ACTIVE 行が**実 ADB に記録される**
      （SELECT で証跡化）。`issue_token` が **rag.search だけを載せた**トークンを発行 → verify で scope claim が
      `platform:rag.search` のみ（db.query は載らない）→ authorize 通過で `platform_broker_audit` に ALLOW 行。
- [ ] シナリオ2（承認境界・失効・越境 拒否系）: (a) 承認超過（db.query 要求）→ `scope_not_granted` で
      **トークン未発行**、(b) manifest 非要求スコープの承認 → 拒否、(c) `revoke_grant` 後の `issue_token` →
      `grant_revoked` で未発行（grant 行が REVOKED へ）、(d) 別テナント T2（グラント無し）→ `no_grant`。
      grant 行の状態遷移（ACTIVE→REVOKED）と各拒否を DB／出力で証跡化。
- [ ] 実施不能な範囲（実 Platform API ルート本体＝PAPI-03、承認 UI の画面操作＝フロント未実装、OIDC＝INFRA-02）は
      `runs/<run-id>/e2e/SKIPPED.md` に理由明記。

## 非ゴール / 制約
- 実 Platform API ルート（rag.search/db.query 等の本体）は PAPI-03。本タスクは**承認＋発行フロー**に限定。
- 承認 UI の画面実装は後続。本タスクは承認 API（関数）＋永続化＋発行関数まで。
- 認証情報・テナンシ/コンパートメント OCID・エンドポイント実値・**ブローカー署名鍵をコミットしない**
  （`platform_broker_secret` は .env / Vault 注入）。
- spec-driven: 仕様にない判断は実装せず ADR に書く。コミット/PR/push は人間承認後。
