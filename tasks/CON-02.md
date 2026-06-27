# タスク: CON-02 — Slack コネクタ（コア）＋コネクタ実行(invoke)層

## ゴール
CON-01 で確定したコネクタ配布表現（`kind: connector` の定義・合成バリデーション・登録）の上に、
**登録済みコネクタの action を実際に呼び出す実行経路（invoke 層）** と、その最初の実体となる
**コア同梱 Slack コネクタ（builtin transport）** を実装する。コネクタは「DB 認証情報を持たずに
外部 SaaS／テナントデータへ到達する唯一の正規経路」(plan §4-3) の L2 を担うため、invoke は必ず
**Platform API ブローカー（PAPI-01）による認可（`platform:connector.invoke` ＋ action 宣言スコープ）を
fail-closed で強制**し、許可/拒否を `platform_broker_audit` に記録する。**実 Slack 認証は本タスクでは
投入しない**＝差し替え可能な transport を mock して投稿フローを検証する（実シークレットは持たない・
secret_ref は参照名のみ。実 Vault 束ねは install 時＝CON-03 領域）。MCP transport（Responses API
type:"mcp"）の呼び出し配管も同じ invoke 層に載せ、単体は mock、実 MCP/実 Slack 接続は SKIPPED に明記する。

## 対象 area
api

## 依存
CON-01（connector 定義/store/migration 019）＋ PAPI-01（platform_broker / migration 020）。base=feat/stage-3。
後続: CON-03（合成 sample-app × AI 部品 × connector への組込＋本格 E2E・実 Vault 束ね・実 SaaS 接続）。

## 仕様参照
specs/16-platform.md §12（CON-01）/ docs/enhance/202607-demo-platform-plan.md §6 D9・§10・§4-3・§7 /
docs/decisions/ADR-0014（broker）。Responses API type:"mcp" の実機確定は mcp_servers.py / chat.py。

## 受け入れ条件（検証可能な述語で書く）
- [ ] `jetuse_core/plugins/connector_runtime.py`: `invoke_connector_action(definition, action, payload, *,
      broker_token, tenant, secret_resolver=None, ...)` を実装。手順は **(1) action 存在検証 →
      (2) ブローカー認可（必須 `platform:connector.invoke` ＋ action.permissions の各 Platform スコープを
      `platform_broker.authorize` で強制。許可/拒否は `platform_broker_audit` に記録）→ (3) 秘密解決
      （auth.kind!=none のとき secret_resolver(secret_ref) でトークン取得）→ (4) transport 別ディスパッチ**。
- [ ] **fail-closed**: 認可失敗（未付与スコープ/テナント越境/期限切れ/改竄/鍵未設定）は
      `ConnectorInvokeDenied`(または broker 例外)に倒し、**外部副作用（transport 呼び出し）を一切起こさない**。
      認可は外部呼び出しより前に行う（拒否時に Slack/MCP へ到達しないことをテストで保証）。
- [ ] **実シークレットを保持/記録しない**: 解決したトークンは戻り値・例外・監査・ログに出さない。
      secret_resolver 未設定で auth.kind!=none を invoke したら fail-closed（`ConnectorInvokeError`）。
- [ ] **Slack コア（builtin）**: `jetuse_core/plugins/slack_connector_builtin.py` に
      コア同梱 Slack コネクタ定義（transport=builtin / provider=slack / actions: `post_message` 等 /
      auth=oauth2・secretRef=参照名）と、`(slack, post_message)` の builtin ハンドラを実装。
      ハンドラは **差し替え可能な HTTP transport** 経由で `chat.postMessage` 要求を組み立てる
      （既定 transport は実ネットワーク禁止の fail-closed。テスト/E2E は mock を注入）。
      payload 検証（channel/text 必須・長さ上限）。Slack 定義は `validate_connector` ／
      `validate_connector_composition`(ok) ／ `validate_manifest` を満たす。
- [ ] **MCP transport（汎用）**: transport=mcp のコネクタは Responses API `type:"mcp"` ツール仕様
      （server_label=provider / server_url=endpoint / Bearer=解決トークン / require_approval=never）を
      組み立てて `responses.create` を呼ぶ配管を持つ（呼び出し本体は差し替え可能＝単体は mock）。
- [ ] **新規 migration は作らない**（invoke の認可監査は既存 `platform_broker_audit`(020) を再利用＝
      むやみにリソースを増やさない）。既存の公開シグネチャ（CON-01/PAPI-01）を壊さない。
- [ ] `.venv/bin/pytest packages/api/tests` 全件パス（正常系 builtin 投稿／mcp 配管／認可拒否で外部不到達／
      秘密未設定 fail-closed／秘密が監査・戻り値に出ない、を網羅）。`.venv/bin/ruff check packages/api` クリーン。
- [ ] `specs/16-platform.md` に §12.6「コネクタ実行(invoke)／Slack コア（CON-02）」を追記（spec 昇格）。

## E2E シナリオ（実環境 / jetuse-dev・固定 loop ADB・最低2本）
完了ゲートで Claude が jetuse-dev の固定 loop ADB へ接続し、専用スキーマ `JETUSE_CON_02`(schema isolation)で
下記を実行して証跡を `runs/<run-id>/e2e/` に残す。Codex は実行せず証跡＋diff を評価する。
- [ ] シナリオ1（正常系・builtin Slack 投稿）: 専用スキーマで 019/020 適用 → コア Slack コネクタを
      `register_connector` → `connector_instances` に出現（SELECT 確認・**定義 CLOB に実トークンが無く
      secretRef のみ**）→ `platform:connector.invoke` を付与した短期トークンを発行 →
      `invoke_connector_action(post_message)` を **mock HTTP transport** で実行し成功 → 実 ADB の
      `platform_broker_audit` に **ALLOW 行**（resource=marker）が記録される（SELECT で証跡化）。
- [ ] シナリオ2（異常系・fail-closed）: (a) `connector.invoke` 未付与トークンで invoke → 拒否され
      **mock transport が呼ばれない**（外部副作用ゼロ）＋ `platform_broker_audit` に **DENY 行**、
      (b) 別テナント越境トークンで invoke → tenant_mismatch DENY。各 DENY が監査に残ることを SELECT で確認。
- [ ] 冪等性: 019/020 の再適用が no-op（schema_migrations で既適用スキップ）であることを確認。
- [ ] 実施不能（**実 Slack 投稿＝実 OAuth トークン未投入** / **実 MCP サーバー接続＝Responses type:"mcp" の
      実エンドポイント未配備**）は `runs/<run-id>/e2e/SKIPPED.md` に理由明記（mock で検証した範囲も併記）。

## 非ゴール / 制約
- 合成（sample-app × AI 部品 × connector）への組込・install 時の実 Vault 束ね・実 SaaS/実 MCP 接続は CON-03。
- スコープ承認 UI・トークン発行フロー本実装は PAPI-02、実 Platform API ルート本体は PAPI-03。
- 認証情報・OCID・エンドポイント実値・**実シークレット／実トークンをコミットしない／証跡に書かない**。
- 既存リソース（VCN develop / インスタンス dev / バケット）は参照のみ。jetuse-dev の固定 loop ADB を再利用。
- spec-driven: 仕様にない判断は実装せず docs/decisions/ に ADR 案。コミット/PR/push は人間承認後。
