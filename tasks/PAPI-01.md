# タスク: PAPI-01 Platform API ブローカー設計ADR＋スパイク

## ゴール
DB認証情報を渡さずにテナントデータへ到達する唯一の正規経路＝**Platform API ブローカー**（§7）の
認可モデルを ADR-0014（ドラフト）で確定し、その中核（**スコープ付き短期トークンの発行・検証・
スコープ強制・テナント境界・監査**）を**スパイク実装**として動かし、実環境で最小実証する。

## 対象 area
api ＋ docs

## 依存
S2 完了（feat/stage-3 ベース）。後続: PAPI-02（スコープ承認＋発行フロー）/ PAPI-03（実 API 実装）。

## 仕様参照
docs/enhance/202607-demo-platform-plan.md §7・§4（ガバナンス4制約）・§2 D5/D8 / docs/decisions/ADR-0013

## 受け入れ条件（検証可能な述語で書く）
- [ ] docs/decisions/ADR-0014（**ドラフト = status: Proposed**）を起票：スコープ語彙／短期トークン
      （テナント=Project OCID・プラグインID・付与スコープ内包）／テナント境界／fail-closed 検証／
      監査・レート制限の方針。**承認は人間ゲート（このタスクでは越えない）**。
- [ ] jetuse_core/platform_broker.py が、短期トークンの **発行（issue）/検証（verify, fail-closed）/
      スコープ強制（authorize）/テナント一致** を提供する（スパイク；実 API ルートは PAPI-03）。
- [ ] スコープ語彙は manifest の `PLATFORM_SCOPES`（platform:rag.search 等）と一致し、付与スコープは
      その部分集合のみ受理する。未知スコープ・改竄・期限切れ・テナント不一致は拒否（fail-closed）。
- [ ] migration（`platform_broker_audit`）で全アクセスの監査行（テナント/プラグイン/スコープ/許否/jti）を
      記録できる。再適用は冪等（既適用は no-op）。
- [ ] `.venv/bin/pytest packages/api/tests` 全件パス（broker の正常系＋拒否系を網羅）／
      `.venv/bin/ruff check packages/api` クリーン。既存の公開シグネチャを壊さない。

## E2E シナリオ（実環境 / jetuse-dev・固定 loop 環境・最低2本）
完了ゲートで Claude が jetuse-dev の固定 loop 環境へデプロイ（`ops/start-adb-if-stopped.sh` ＋
`python -m jetuse_core.migrate`）し、spike スクリプト（`spikes/spike05_platform_broker.py`）を実環境の
実 ADB に対して実行して証跡を `runs/<run-id>/e2e/` に残す。
- [ ] シナリオ1（正常系）: ブローカーがテナント T・プラグイン P へ `platform:rag.search` を付与した
      短期トークンを発行 → 検証成功 → スコープ強制を通過 → **実 ADB の `platform_broker_audit` に
      ALLOW 行が記録される**（DB から SELECT して証跡化）。
- [ ] シナリオ2（拒否系・テナント境界/期限）: (a) 別テナント T2 のリソースへのアクセスを拒否、
      (b) 期限切れ/改竄トークンを fail-closed で拒否、(c) 未付与スコープを拒否 → 各々 **DENY 行が
      `platform_broker_audit` に記録される**（越境が監査に残ることを実環境で確認）。
- [ ] 実施不能な範囲（実 API ルート rag.search/db.query 本体＝PAPI-03 で実装、OIDC 連携＝INFRA-02）は
      `runs/<run-id>/e2e/SKIPPED.md` に理由明記。

## 非ゴール / 制約
- 実 Platform API ルート（rag.search/db.query/conversations/files/connector.invoke の本体）は PAPI-03。
  本タスクは**認可基盤のスパイク**に限定（トークン＋スコープ＋テナント＋監査の配管）。
- スコープ承認 UI・発行フローの本実装は PAPI-02。本タスクは発行関数まで。
- 認証情報・テナンシ/コンパートメントOCID・エンドポイント実値・**ブローカー署名鍵をコミットしない**
  （`platform_broker_secret` は .env / Vault 注入）。
- spec-driven: 仕様にない判断は実装せず ADR に書く。コミット/PR/push は人間承認後。
- 人間ゲート: **ADR-0014 の承認**（ドラフト作成までで停止）。
