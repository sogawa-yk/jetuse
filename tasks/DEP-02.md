# タスク: DEP-02 — 生成デモへの Platform API 注入(L3 ホスト型 / D3 解)

> 起票: DEP-01 完了後に本ループ(2026-06-27)で起票。ADR-0015 §7 が「デモ配備のライフサイクル
> (更新/破棄/命名規約)・実トークン発行/失効は実 apply 前に DEP-02 で確定する」と残した残課題を確定する。

## ゴール

DEP-01 が生成する **秘密を持たない配備仕様**(`ContainerDeploySpec`)を起点に、デモコンテナ起動時の
**Platform API ランタイム注入**を成立させる。コンテナへ注入するのは **(1) Platform API ベース URL
(非秘密)** と **(2) `platform_grants.issue_token` 発行の承認スコープに厳密に閉じた短期トークン(秘密)**
の 2 つだけ。**DB 認証情報は注入しない**(D5: ブローカー経由の短期トークンのみがテナントデータ経路)。
トークンの失効/更新方針、デモ配備のライフサイクル(更新/破棄/命名規約)を ADR-0015 §7 に従い確定する。
実 `terraform apply`(実コンテナ作成)は人間ゲート(plan/validate 止まり)。

## 対象 area

api(主) + infra。test_cmd=`.venv/bin/pytest packages/api/tests` / lint=`.venv/bin/ruff check
packages/api` / infra=`terraform fmt -check && terraform validate`(hosted-demo 環境)。

## 依存・再利用(新規の実行基盤・認可経路は作らない)

- **DEP-01 / `deploy.py`**: `ContainerDeploySpec`(`required_scopes` / `module_environment()` / 非秘密 env)。
- **PAPI-02 / `platform_grants.issue_token`**: 承認グラントに閉じた短期 JWT 発行(no_grant / grant_revoked /
  scope_not_granted で fail-closed)。
- **PAPI-01 / `platform_broker`**: トークン発行/検証/スコープ強制/TTL 上限(900s)。
- **PAPI-03 / `/platform/*`**: 短期トークンを提示して疎通する実 Platform API ルート(db.query / connector.invoke /
  rag.search)。`authorize` が検証 → scope → テナント一致 → 監査。
- **ADR-0014 / ADR-0015**: ブローカー一本化・秘密の経路分離・L3 は DB 資格を持たない。base=feat/stage-4。

## 受け入れ条件(検証可能な述語で書く)

- [ ] `jetuse_core/deploy_inject.py`(新規): `build_runtime_injection(spec, *, tenant, plugin_id, ...) ->
      RuntimeInjection` を提供。`RuntimeInjection.env()` は **非秘密のみ**(`JETUSE_PLATFORM_API_BASE_URL`)、
      `secret_env()` は **トークンのみ**(`JETUSE_PLATFORM_TOKEN`)。`expires_at` / `seconds_remaining()` /
      `should_refresh()` で失効・更新を表現。`redacted()` はトークンを伏せる。
- [ ] **DB 認証情報を注入しない**: 注入物のキーは allowlist(`{JETUSE_PLATFORM_API_BASE_URL}` /
      `{JETUSE_PLATFORM_TOKEN}`)に限定。`adb_*` 等の DB 資格を読まない・載せない(構造的に到達不能)。
- [ ] **承認スコープに厳密に閉じる(二重閉包・fail-closed)**: トークンスコープは配備仕様 `required_scopes`
      の部分集合(宣言外は `scope_outside_spec` で拒否)かつ承認グラントの部分集合(承認超過は
      `scope_not_granted`、無し=`no_grant`、失効=`grant_revoked`)。発行直後に `verify_broker_token` で
      自己検証し、載ったスコープ・失効時刻を権威値として確定。
- [ ] **ベース URL 検証**: https 固定(平文 http 不可)・Vault OCID 混入拒否・空は fail-closed。
      解決は引数優先、無ければ `settings.platform_api_base_url`。
- [ ] **秘密/非秘密の経路分離(短期トークンを state に残さない)**: `hosted-demo` 環境は **非秘密の**
      `platform_api_base_url` のみ Terraform 経由で注入する。**短期トークンは Terraform を通さない**
      (Terraform に渡した値は resource 入力として state に残るため)。トークンは起動時のアウトオブバンド
      注入(`secret_env()` をオーケストレータが実行中コンテナへ直接注入 / 将来はコンテナ自己取得)で渡す。
      `terraform fmt`/`validate` クリーン。
- [ ] `settings.platform_api_base_url`(新規・空既定)。
- [ ] `.venv/bin/pytest packages/api/tests` 全件パス・`.venv/bin/ruff check packages/api` クリーン。
- [ ] 既存の公開シグネチャ(deploy/platform_grants/platform_broker/container-instance モジュール・DEP-01 の
      committed tfvars 契約)を壊さない(追加のみ)。
- [ ] **ADR-0016** をドラフト作成(注入・トークン失効/更新・命名規約・更新/破棄)。状態=提案中(承認は人間ゲート)。

## E2E シナリオ(実環境 / jetuse-dev・最低2本)

完了ゲートで Claude が下記を実行し証跡を `runs/<run-id>/e2e/` に残す。Codex は実行せず証跡＋diff を評価する。
実 ADB は固定の **loop ADB**(jetuse-dev / 再利用)を使い、業務分離は **専用スキーマ `JETUSE_DEP_02`**
(承認グラント `platform_scope_grants` ＋ 監査 `platform_broker_audit` を当該スキーマに置く)で行う。

- [ ] **シナリオ1(注入 → Platform API 疎通・正常系)**: 実 loop ADB(`JETUSE_DEP_02`)に承認グラントを
      `approve_scopes`(tenant=Project OCID / plugin=デモ / scope=`platform:connector.invoke`)→ 代表構成
      (SBA-A + Slack)を `synthesize()` → `build_deploy_spec()` → `build_runtime_injection()` で
      base_url ＋ 短期トークンを得る(= mock コンテナ/ローカルプロセスへの注入)→ そのトークンを
      `Authorization: Bearer` で実 Platform API(`/platform/connector/invoke`)へ提示。**期待**: authorize を
      通過し(scope/テナント一致)、`platform_broker_audit` に **ALLOW** が記録される。注入物に DB 認証情報・
      Vault OCID・トークンの非秘密 env 漏れが無い。証跡=注入 redacted ＋ HTTP 応答 ＋ 監査行。
- [ ] **シナリオ2(失効/越境/スコープ閉包・拒否系)**: (a) `revoke_grant` 後の再注入が `grant_revoked` で
      失敗、(b) 配備仕様の宣言外スコープ要求が拒否、(c) 別テナントのトークンで `/platform/*` を叩くと
      403 `tenant_mismatch` で監査に **DENY**。**期待**: いずれも fail-closed。証跡=各操作の例外/HTTP 応答
      ＋ 監査行(DENY)。
- [ ] **SKIPPED**: 実 `terraform apply`(課金・実コンテナ作成)・実 OCIR push は人間ゲートのため実施しない →
      `runs/<run-id>/e2e/SKIPPED.md` に理由明記。`terraform validate`(hosted-demo)までは実施。

## 非ゴール / 制約

- 実コンテナ起動・実 apply・OCIR push は本タスク非対象(人間ゲート)。
- 即時失効(jti 失効リスト)・コンテナ自己トークン更新(OIDC 発行主体認証)は INFRA 範囲。
- 既存リソース(VCN develop / インスタンス dev / バケット)は参照のみ。loop ADB は再利用(増やさない)。
- `terraform apply` は絶対にしない(plan/validate 止まり)。コミット/PR/push は人間ゲート。
- spec-driven: specs/ にない判断は実装せず docs/decisions/ に ADR 案(ADR-0016)を書く。
