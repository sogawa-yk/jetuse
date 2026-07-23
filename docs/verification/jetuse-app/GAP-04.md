# GAP-04 検証レポート: マネージド・ホスト型エージェント統合（案1・完了）

- 日付: 2026-06-13 / ブランチ: `task/gap-04` / 計画: docs/plan-gap-b.md / 調査: SPIKE-G4
- ユーザー判断: **案1（Hosted Applicationを常設しアプリに配線）**

## 完了（自律作業）

- 常設用OCIRリポジトリ `jetuse-dev-hosted-agent` 作成 + イメージ `0.1.0` push（AGT-04のLangGraphサンプル）
- アプリ配線コード: `jetuse_core/hosted_agent.py`（IDCS client_credentials取得+invoke中継、設定外出し）、
  `framework="hosted"` を AgentDefinition / chat_stream（非ストリーミングを1 deltaでSSE化）/ ビルダーUI に追加
- 設定項目 `HOSTED_AGENT_APP_OCID` / `_IDCS_DOMAIN` / `_CLIENT_ID` / `_CLIENT_SECRET` / `_SCOPE`
  （未設定なら framework=hosted は明示エラー）
- テスト2件（未設定エラー / hostedはツール検証スキップ）→ API計99件pass

## 追加実施（2026-06-13、エージェント設定分）

- **IDCS OAuthアプリ `jetuse-agent` を作成**（confidential client + resource兼用、client_credentials、
  audience=jetuse-agent / scope=invoke / fqs=jetuse-agentinvoke、expiry3600）。
  **client_credentialsトークンの aud/scope クレームを実機検証済み**
- 資格情報を tfvars(非コミット)の `HOSTED_AGENT_*` に格納。設定解説=docs/setup/hosted-agent-oauth.md

## 残（エージェント権限では実施不可＝権限境界）

- **動的グループ `jetuse-dg` の2タイプ追加**（generativeaihostedapplication/deployment）は、
  `jetuse-dg`がテナンシ直下のDefaultドメインにあり**本認証情報ではアクセス不可(404 NotAuthorized)**。
  プロジェクト承認ルールでなく実IAM権限境界。Defaultドメイン管理権限を持つ主体が実施する必要。
  これが入るまでHosted DeploymentのイメージpullがFAILEDになるため、デプロイは保留
- `read repos` ポリシーは既存確認（AGT-04で追加済みの可能性）

## 残（人間作業後にエージェントが実施）

- `ops/deploy-hosted-agent.sh`（常設用に調整: repo=jetuse-dev-hosted-agent / audience=jetuse-agent）で
  Hosted Application + Deployment を常設デプロイ
- tfvars/`.env` の `HOSTED_AGENT_*` に APP OCID + IDCS資格情報を設定して再デプロイ
- E2E: framework=hosted のエージェントをアプリから実行→マネージド・エージェントの応答を確認
- comparison/aws-reference.md のB項目「AgentCore相当」を「マネージドで実装済み」に更新


## 完了（2026-06-15、動的グループ追加済みを確認しデプロイ〜E2E）

- Hosted Application `jetuse-dev-hosted-agent` + Deployment を常設デプロイ → ACTIVE/ACTIVE
  （イメージpull成功＝動的グループ `jetuse-dg` の2タイプが有効）
- 直接invoke: `/health` 200 / `/invoke` 正答（OAuth client_credentials, aud=jetuse-agent/scope=invoke）
- `HOSTED_AGENT_APP_OCID` をtfvarsに設定→apply。**アプリ経由(framework=hosted)でE2E成功**
  （「OCI Functionsの利点を要約」を正しく応答、error=None）
- OAuth設定解説: docs/setup/hosted-agent-oauth.md
