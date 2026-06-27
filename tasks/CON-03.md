# タスク: CON-03 — 合成への組込（sample-app × AI部品 × connector）＋ connector 束縛/invoke 経路

## ゴール
CON-01（コネクタ配布表現・登録）／CON-02（invoke 層・コア Slack）／PAPI-01..03（broker・grants・
`/platform/*`）の上に、**合成エンジン(synth)とデプロイ前ガバナンス(governance)へコネクタを正式に組込む**。
これまで synth は推薦の `connectors` を文字列のまま転記していただけ（束縛していない）。本タスクで
**コネクタを AI 部品と同じく「束縛」**し（コアパレットに在るか・合成整合が取れるかを判定して
active/excluded を理由付きで残す）、governance は **コアパレット(Slack)＋`platform:connector.invoke`
スコープ経路**をデプロイ前ゲートで検証する。実呼び出しは **Platform API ブローカー(connector.invoke)
経由の短期 JWT＋監査**（CON-02 の invoke 層を再利用）で、合成された active コネクタを実際に叩く所まで
E2E で通す。**実 Slack 認証は今回投入しない**＝mock transport で broker 経由 invoke を検証し、実 Slack は
`SKIPPED.md` に明記する。

## 対象 area
both（api を主とし、web は build 緑のみを確認。UI 改修は本タスク非ゴール）

## 依存
CON-01（connector 定義/store/migration 019）＋ CON-02（connector_runtime / slack_connector_builtin）＋
PAPI-01..03（platform_broker / platform_grants / `/platform/*`）。base=feat/stage-3。

## 仕様参照
specs/16-platform.md §12（CON-01/02）/ docs/enhance/202607-demo-platform-plan.md §4-3・§6 D9・§10 /
HBD-03(synth) / HBD-04(governance)。新規 migration は作らない（既存 019/020 を再利用）。

## 受け入れ条件（検証可能な述語で書く）
- [ ] `jetuse_core/plugins/core_connectors.py`（新規・小）: コアコネクタの **provider→定義/manifest**
      レジストリ（現状 Slack 1本）。`core_connector(provider)` / `core_connector_providers()` /
      `connector_invoke_scopes(definition)`（= `platform:connector.invoke` ＋ action 宣言 Platform スコープの
      和集合・順序固定）/ `resolve_active_connector(composition, provider)` を提供。**実シークレットを持たない**。
- [ ] `synth.py`: `ConnectorBinding`（provider/status(active|excluded)/transport/actions/required_scopes/
      requires_secret/secret_ref/reason）を追加し、`synthesize()` が推薦 `connectors` を**束縛**する。
      active = コアパレットに在り、かつコネクタ合成バリデーション(`validate_connector_composition`)が ok。
      excluded = パレット外、または合成不整合（理由付き・warnings へ）。`DemoComposition` に
      `connector_bindings` ＋ `active_connectors` を追加（**既存 `connectors: list[str]` は壊さず維持**＝
      summary.py 等の後方互換）。副作用なし・決定的。
- [ ] `governance.py`: コアパレット判定を `core_connector_providers()` から導出（`CORE_CONNECTORS` 名は
      後方互換で維持）。**新たに `platform:connector.invoke` スコープ経路を検証**する `connector_scope`
      チェックを追加: active コネクタの `required_scopes` が既知 Platform 語彙(`PLATFORM_SCOPES`)の部分集合で
      かつ invoke スコープを含むこと、パレット内なのに excluded（合成不整合）なコネクタを違反として弾く
      （`connector_scope_undeclared`／`connector_scope_unknown`、各々**代替提案つき**）。
- [ ] **invoke は Platform API ブローカー経由**: 合成された active コネクタは CON-02 の
      `invoke_connector_action`（`platform:connector.invoke` ＋ action スコープを broker 強制・
      `platform_broker_audit` 記録・fail-closed）で呼ぶ。新しい invoke 経路は作らない（再利用）。
- [ ] `.venv/bin/pytest packages/api/tests` 全件パス（connector 束縛 active/excluded／governance の
      connector_scope 合否／パレット外拒否／合成→ガバナンス→invoke の結線）。`.venv/bin/ruff check
      packages/api` クリーン。`npm --prefix packages/web run build` 成功。
- [ ] `specs/16-platform.md` に §12.7「合成への組込／connector 束縛・invoke 経路（CON-03）」を追記（spec 昇格）。
- [ ] 既存の公開シグネチャ（synth/governance/connector/broker）を壊さない（追加のみ）。

## E2E シナリオ（実環境 / jetuse-dev・固定 loop ADB・最低2本・JETUSE_CON_03 隔離）
完了ゲートで Claude が jetuse-dev の固定 loop ADB へ接続し、専用スキーマ `JETUSE_CON_03`（schema isolation）で
下記を実行して証跡を `runs/<run-id>/e2e/` に残す。Codex は実行せず証跡＋diff を評価する。
- [ ] シナリオ1（正常系・合成→ガバナンス→invoke）: 推薦（Slack 連携あり）→ `synthesize()` で
      **connector_bindings に slack=active**（required_scopes に `platform:connector.invoke`）→
      `validate_governance()` が **ok（connector_scope パス）** → 専用スキーマで 019/020 適用 →
      コア Slack コネクタを `register_connector`（CLOB に secretRef のみ・実トークン無し）→
      `platform:connector.invoke` 付与の短期トークンで合成 active コネクタを `invoke_connector_action`
      （**mock HTTP transport**）→ 成功 ＋ 実 ADB `platform_broker_audit` に **ALLOW 行**（resource=marker）。
- [ ] シナリオ2（異常系・パレット外＋fail-closed）: (a) パレット外コネクタ(teams 等)を含む構成 →
      governance が `disallowed_combination`／active にならず excluded（invoke 不可）、(b) `connector.invoke`
      未付与トークンで invoke → 拒否され **mock transport が呼ばれない**＋`platform_broker_audit` に
      **DENY 行**、(c) 別テナント越境トークンで invoke → tenant_mismatch DENY。各 DENY を SELECT で確認。
- [ ] 冪等性: 019/020 の再適用が no-op（schema_migrations で既適用スキップ）。
- [ ] 実施不能（**実 Slack 投稿＝実 OAuth トークン未投入** / **実 Vault 束ね** / **実 MCP/実 SaaS 接続**）は
      `runs/<run-id>/e2e/SKIPPED.md` に理由明記（mock で検証した範囲も併記）。

## 非ゴール / 制約
- 実 Vault 束ね・実 Slack/実 MCP 接続・install 時の秘密束ね本実装は本タスク非ゴール（後段）。mock で検証。
- UI（プレビュー画面でのコネクタ表示改修）は本タスク非ゴール（synth/governance のモデル拡張に留める）。
- 認証情報・OCID・エンドポイント実値・**実シークレット／実トークンをコミットしない／証跡に書かない**。
- 既存リソース（VCN develop / インスタンス dev / バケット）は参照のみ。jetuse-dev の固定 loop ADB を再利用。
- spec-driven: 仕様にない判断は実装せず docs/decisions/ に ADR 案。コミット/PR/push は人間承認後。
