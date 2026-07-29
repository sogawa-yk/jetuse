# ADR-0019: 公開スタックでホスト型エージェントを3SDKすべて既定配備し、ゼロスケール前提で運用する

日付: 2026-07-29
状態: 承認済み（2026-07-29 ユーザー決定。PORT-03 / Issue #93。実装 PR merge をもって確定）

## 背景

公開 ORM スタックからデプロイした環境では、エージェント機能が次のエラーで一切使えない。

```
agent container not configured: sdk=openai_agents
missing=['hosted_agent_idcs_domain', 'hosted_agent_client_id',
         'hosted_agent_client_secret', 'hosted_agent_scope']
```

`docs/setup/hosted-agent-oauth.md` が AGT-04 当時の**手動デプロイ前提**で書かれていたため、
「ホスト型アプリは Terraform で作れない」という前提が残っていた。調査の結果それは成り立たない。

### 確定した事実（2026-07-29 / スキーマ検証・実機検証）

**1. Terraform で宣言的に組める。** ORM が入れる OCI provider **v8.24.0** に必要な resource が揃う。

| 必要なもの | Terraform resource |
|---|---|
| ホスト型アプリ | `oci_generative_ai_hosted_application`（`inbound_auth_config.idcs_config{audience, domain_url, scope}`） |
| コンテナ配備 | `oci_generative_ai_hosted_deployment`（`active_artifact{container_uri, tag, artifact_type}`） |
| OAuth クライアント兼リソース | `oci_identity_domains_app`（`client_type=confidential` / `is_oauth_resource` / `audience` / `scopes`） |
| client_secret のアプリへの受け渡し | 同上 — **`client_secret` は computed 属性**なので Terraform から env へ注入できる |

`inbound_auth_config.idcs_config` が AGT-04 で手作業していた inbound 検証（aud/scope 突合）そのもの。

**2. ゼロスケールする。** [Application comparison](https://docs.oracle.com/en-us/iaas/Content/generative-ai/application-comparison.htm)
は GenAI Applications を **Scale to zero: Yes / "Automatic scaling from 0 to many instances"**
（serverless-style deployment）と明記している。実機（us-chicago-1）でも
`scaling_type=CONCURRENCY, min_replica=0, max_replica=2` で作成が成功し、**ACTIVE 到達後も
`min_replica=0` が保持される**ことを確認した。

**3. API はGenAI実証済みリージョンで使える。** `list_hosted_applications` が
us-chicago-1 / ap-osaka-1 とも HTTP 200。

### 検討時に誤っていた前提（記録）

当初「`scaling_config` に `min_replica` がある ＝ 常設課金」と推測し、OpenSearch
（`enable_opensearch` 既定 OFF）と同じ費用論点があるとして「既定 OFF」案を有力に挙げていた。
**ドキュメントにも API にも当たらない推測で、誤りだった。** ゼロスケールが効くため費用論点は消える。

## 決定

### 1. 3SDK（openai_agents / langgraph / adk）を**すべて既定で配備**する

`min_replica=0` ならアイドル時のコストが発生しないため、SDK 数を絞る費用上の理由が無い。
3つ揃えることで `docs/comparison/agent-frameworks.md` の**SDK 切替デモが既定構成のまま成立**する
（`docs/guides/demo-scenarios.md` シナリオ4）。

判断材料は課金ではなく次の2点であり、実装時に実測して問題があれば再検討する。

- テナンシの service limit（quota）消費
- apply 時間の伸び

### 2. `scaling_config` の既定は `min_replica=0`

公開スタックの費用前提は「使った分だけ」。デモ等でコールドスタートを避けたい利用者向けに、
スタック変数（例 `hosted_agent_min_replica`、既定 `0`）で上げられるようにする。

### 3. コールドスタートは実測して扱いを決める

`min_replica=0` では初回 invoke に起動待ちが入る。`jetuse_core/hosted_agent.py` の
httpx timeout は現行 **180 秒**。

- 実測して 180 秒で不足するなら延長する
- UI に「初回は起動に時間がかかる」旨を出すかは実測値を見て判断する

### 4. `enable_auth=false` では配備しない

OAuth の発行元（Identity Domain）が存在しないため成立しない。この構成では
`GET /api/health` の `capabilities.agents` を `unavailable` + 理由付きで返し、
UI も生の `agent container not configured` ではなく理由を出す（PORT-02 の縮退方針を踏襲）。

## 影響

- `release.yml` が `jetuse-agent-{openai,langgraph,adk}` を4リージョンの OCIR へ push する
  （公開 OCIR に public リポジトリの**事前作成が必要** — ADR-0011 と同じ理由）
- runtime Dynamic Group に `generativeaihostedapplication` / `generativeaihosteddeployment` を追加
- `infra/orm` が `HOSTED_AGENT_*` と `AGENT_{OPENAI,LANGGRAPH,ADK}_APP_OCID` を配線
- `docs/cost-estimate.md` は**アイドル課金なし・従量のみ**として反映
- `docs/setup/hosted-agent-oauth.md` は「dev 環境の手動手順の記録（当時）」と位置づけ、
  公開スタックでは Terraform が自動化する旨へ更新

## 却下した案

| 案 | 却下理由 |
|---|---|
| 既定 OFF（`enable_hosted_agents=false`） | 常設課金という前提が誤りだったため、OFF にする理由が消えた。既定デプロイでエラーが出続ける体験も悪い |
| 既定 ON だが openai_agents 1つだけ | 同上。SDK 切替デモが既定で成立しなくなる副作用だけが残る |
| ホスト型をやめて Container Instance に載せる | ゼロスケールが効かず常設課金になる。ADR-0009 の3SDK構成を作り直す必要もある |

## 実装

`tasks/PORT-03.md`。完了条件はクリーンなテナンシでの**手動作業ゼロ**の apply と、
実機でのエージェント実行・コールドスタート実測・destroy 成功。

## 追記（2026-07-29 実装時）: 作成手段を Terraform resource から OCI CLI へ変更

**決定**: Hosted Application / Deployment の作成・削除は `oci raw-request`（`terraform_data` +
local-exec）で行い、OCID の参照だけ data source から取る。人間の承認済み（2026-07-29）。

**理由**: oracle/oci **8.24.0（当時の最新）の `oci_generative_ai_hosted_application` は必ず失敗する**。
サービス側は作成・削除とも成功し、work request も `SUCCEEDED`・リソースも `ACTIVE` になるが、
provider の `hostedApplicationWaitForWorkRequest` が work request の resources を
`strings.Contains(strings.ToLower(res.EntityType), "hostedapplication")` で照合する一方、
サービスが返す `entityType` は `HOSTED_APPLICATION`（アンダースコア入り）で一致しない。
`identifier` が nil のまま失敗扱いになる。しかも失敗したリソースは tainted になり、
次の apply が「削除 → 再作成」を試みて delete 側でも同じ誤判定を起こすため**収束しない**。
再現手順と実測は `docs/verification/PORT-03.md`。

**この変更は暫定である。provider が修正されたら、標準の resource による宣言的な構成へ戻す。**

戻す条件と手順:

1. `oci_generative_ai_hosted_application` / `_hosted_deployment` を含む provider で、
   `entityType` の照合が修正されていること（リリースノートまたはソースで確認する）。
   確認箇所は `internal/service/generative_ai/generative_ai_hosted_application_resource.go` の
   work request 待ち受け。
2. `infra/terraform/modules/hosted-agent/` の `terraform_data.agent` と `scripts/` を、
   `oci_generative_ai_hosted_application` / `_hosted_deployment` へ置き換える。
   参照用の data source は不要になる（resource の属性を直接使える）。
3. 既存スタックのために state 移行（`import` もしくは `removed` ブロック）を用意する。
   CLI で作ったリソースは Terraform state に載っていないため、**単純な置き換えでは
   重複作成になる**。
4. `docs/verification/PORT-03.md` と同じ E2E（apply → 3SDK 実行 → destroy）で再確認する。

追跡は **Issue #98**（provider 修正後にホスト型エージェントを標準 resource へ戻す）で行う。

### 併せて変更した点

- **コンテナ設定は `JETUSE_AGENT_CONFIG`（JSON オブジェクト1本）で渡す。**
  provider は `environment_variables.value` を「JSON として妥当な文字列」しか受け付けないのに
  アンマーシャルせず送り、API も verbatim 保存するため、スカラーを渡すと**引用符が値に残る**
  （実機確認）。オブジェクトなら往復が一致する。コンテナ側は `agent_env.py` が展開する。
- **コンテナ画像のタグは `image_tag` に統一する**（API / Functions ルーター / 3SDK エージェント）。
  エージェントだけ差し替えたら API が旧版のままで `capabilities.agents` が出なかった。
  両者は invoke ステート（`project_ocid` 等）の契約を共有するため、別リリースに割ってはいけない。
  配布 zip 生成時に commit SHA へ固定する。
- **runtime Dynamic Group には `generativeaihostedapplicationiam` も必要**（公式の配備要件）。
  併せて `read repos` と `read vss-family` を付与する。
