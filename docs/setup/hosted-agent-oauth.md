# マネージド・ホスト型エージェントのOAuth/認証設定（GAP-04）

アプリ→OCI Hosted Application（マネージドにホスティングされたエージェント）を invoke するための
**OAuth 2.0 client_credentials フロー**の設定解説。2026-06-13にエージェントが設定した内容を記録する。

## 全体像（OAuthフロー）

```
[本アプリ(CI/FastAPI)]
   │ ① client_credentials grant (client_id + secret, scope=jetuse-agentinvoke)
   ▼
[IDCS / Identity Domain  jetuse-dev-domain  /oauth2/v1/token]
   │ ② access_token (JWT, aud=jetuse-agent, scope=invoke)
   ▼
[OCI Hosted Application invoke エンドポイント]
   │ ③ Authorization: Bearer <token>
   │   Hosted Applicationの inbound-auth(IDCS_AUTH_CONFIG)が
   │   token の aud / scope を audience=jetuse-agent / scope=invoke と突合して検証
   ▼
[コンテナ(LangGraphエージェント) /invoke]  → 応答
```

- **資源サーバ(OAuth resource)** と **クライアント(OAuth client)** を**1つのIDCSアプリ `jetuse-agent` で兼用**
  している。これにより、アプリは自分自身が発行する token（aud=自分=jetuse-agent）で
  自分が保護する invoke を呼べる（client_credentials の自給自足構成）。

## ① 設定済み: IDCS OAuthアプリ `jetuse-agent`（エージェントが作成）

| 項目 | 値 |
|---|---|
| ドメイン | jetuse-dev-domain（`https://idcs-1a7db50d84bd47acb4ef51b5bcbdf56f.identity.oraclecloud.com`） |
| アプリ種別 | confidential（OAuth client）+ OAuth resource の兼用 |
| 付与グラント | `client_credentials` のみ |
| audience（resource識別子） | `jetuse-agent` |
| scope | `invoke`（**完全修飾スコープ fqs = `jetuse-agentinvoke`**） |
| access token 有効期限 | 3600秒 |
| client_id | `8f675390bc59456c8b4834d1583d25d7` |
| client_secret | tfvars(実機・非コミット)に格納。git管理しない |

**検証済み**: 上記クライアントで client_credentials トークンを取得し、JWTの
`aud=jetuse-agent` / `scope=invoke` / `sub=client_id` を実機で確認。Hosted Applicationの
inbound検証が通る前提条件を満たしている。

## ② 設定済み: アプリ側の格納場所（非コミット）

`infra/terraform/environments/dev/terraform.tfvars` の `api_environment` に以下を格納
（tfvarsはgitignore済み。秘密はリポジトリに入れない方針）:

```
HOSTED_AGENT_IDCS_DOMAIN  = https://idcs-...identity.oraclecloud.com
HOSTED_AGENT_CLIENT_ID    = 8f675390...
HOSTED_AGENT_CLIENT_SECRET= ********（非公開）
HOSTED_AGENT_SCOPE        = jetuse-agentinvoke
HOSTED_AGENT_APP_OCID     = （Hosted Applicationデプロイ後に設定 — 下記④の後）
```

アプリ側コード `jetuse_core/hosted_agent.py` がこの設定を読み、`framework="hosted"` のエージェントで
トークン取得→invoke中継を行う（トークンは期限60秒前まで再利用）。

## ③ 設定済み: コンテナイメージ

OCIRリポジトリ `jetuse-dev-hosted-agent` にエージェントイメージ `0.1.0` をpush済み
（AGT-04のLangGraphサンプル: researcher→summarizer の2ノードグラフ）。

## ④ 未完: エージェントの権限では実施不可（権限境界）

Hosted Application/Deployment の**イメージpullには、ホスト型リソースのリソースプリンシパルが
動的グループ `jetuse-dg` に属し `read repos` 権限を持つ必要がある**（AGT-04実証）。
そのため `jetuse-dg` のマッチングルールへ次の2リソースタイプ追加が必要:

```
all {resource.type='generativeaihostedapplication', resource.compartment.id='<jetuse-protoのOCID>'},
all {resource.type='generativeaihosteddeployment',  resource.compartment.id='<jetuse-protoのOCID>'}
```

> **これはエージェントには実行できない**: `jetuse-dg` はテナンシ直下の Default Identity Domain にあり、
> 本作業で使う認証情報ではそのドメインへ一切アクセスできない（`list_domains` / dynamic group 操作が
> **404 NotAuthorized**）。これはプロジェクトの承認ルールではなく**実際のIAM権限境界**。
> テナンシ/Defaultドメインの管理権限を持つ主体が上記2行を追加する必要がある（反映に5〜10分）。

## ⑤ 上記④の後にエージェントが仕上げる手順

1. `ops/deploy-hosted-agent.sh` を常設用（repo=`jetuse-dev-hosted-agent`, audience=`jetuse-agent`,
   scope fqs=`jetuse-agentinvoke`）に調整して Hosted Application + Deployment をデプロイ
2. 払い出された **Hosted Application OCID** を tfvars の `HOSTED_AGENT_APP_OCID` に設定して `terraform apply`
3. アプリで `framework="hosted"` のエージェントを作成→チャットで実行し、マネージド・エージェントの
   応答を確認（E2E）
4. `docs/comparison/aws-reference.md` のB項目「AgentCore相当」を「マネージドで実装済み」に更新

## 現状サマリ

| 項目 | 状態 |
|---|---|
| OAuthアプリ `jetuse-agent`（client_credentials, aud/scope） | ✅ エージェントが作成・トークン検証済み |
| 資格情報の格納（tfvars, 非コミット） | ✅ |
| エージェントイメージ（OCIR push） | ✅ |
| アプリ配線（hosted_agent.py / framework=hosted / UI） | ✅ |
| 動的グループ `jetuse-dg` の2タイプ追加 | ⛔ **権限境界によりエージェント不可**（Defaultドメイン管理者が実施） |
| Hosted Applicationデプロイ + E2E | ⏸ 上記④の後に実施 |
