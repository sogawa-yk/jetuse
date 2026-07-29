# タスク: PORT-03 公開スタックにホスト型エージェント（Enterprise AI Agent）を載せる

報告: Issue #93 ／ 決定: ADR-0019

**ループ起動**（Public 変更なので **base は `main`**。既定の `dev` ではない）:

```bash
BASE_BRANCH=main GOAL="公開スタックからのデプロイで3SDKのホスト型エージェントが手動作業ゼロで使えること（E2E 38/38 非回帰 + 3SDK実行 + destroy成功）" \
  .claude/loop/start-loop.sh PORT-03
```

## 目的

公開 ORM スタックからデプロイした環境で、**エージェント機能が「未設定」エラーで一切使えない**状態を解消する。
PORT-01（infra の環境依存）/ PORT-02（機能別グレースフルデグレード）と同じ「自環境でだけ動く」問題の、
エージェント面での根治。

利用者が踏むエラー:

```
⚠ エージェント未設定: agent container not configured: sdk=openai_agents
missing=['hosted_agent_idcs_domain', 'hosted_agent_client_id',
         'hosted_agent_client_secret', 'hosted_agent_scope']
```

## 仕様参照

- `specs/11-agents.md` / `specs/14-agent-frameworks.md`
- `docs/decisions/ADR-0009-hosted-react-three-sdk.md`（3SDK汎用ReActコンテナ）
- `docs/setup/hosted-agent-oauth.md`（AGT-04 の OAuth 実証記録。**dev 環境の手動手順**が正本になっている）

## 事前調査で確定済みの事実（2026-07-29 実機・スキーマ検証済み。再検証不要）

### Terraform では作れない、は**もう成り立たない**

`docs/setup/hosted-agent-oauth.md` は「エージェントの権限では実施不可」「手動デプロイ」を前提に
書かれているが、現行の OCI provider（ORM が入れる **oracle/oci v8.24.0**）には必要な resource が揃っている。

| 必要なもの | Terraform resource | 確認した引数 |
|---|---|---|
| ホスト型アプリ本体 | `oci_generative_ai_hosted_application` | `inbound_auth_config.idcs_config{audience, domain_url, scope}` / `environment_variables` / `scaling_config` / `networking_config` |
| コンテナの配備 | `oci_generative_ai_hosted_deployment` | `hosted_application_id`（required）/ `active_artifact{container_uri, tag, artifact_type}`（min_items=1・設定可） |
| OAuth クライアント兼リソース | `oci_identity_domains_app` | `client_type="confidential"` / `is_oauth_resource=true` / `audience` / `scopes{value, fqs}` / `allowed_grants=["client_credentials"]` |
| client_secret のアプリへの受け渡し | 同上 | **`client_secret` は computed 属性**なので Terraform から参照して CI/Functions の env に注入できる |

`inbound_auth_config.idcs_config` が AGT-04 で手作業していた inbound 検証（aud/scope 突合）そのものなので、
**OAuth の自給自足構成（1つの IDCS アプリを client 兼 resource にする）は宣言的に組める**。

### API のリージョン可用性（実測）

`list_hosted_applications` が **us-chicago-1 / ap-osaka-1 のどちらも HTTP 200**。
GenAI 実証済みリージョン（kix/ord）では API 面の障害は無い。

### ゼロスケールできる（＝常設課金ではない）

[Application comparison](https://docs.oracle.com/en-us/iaas/Content/generative-ai/application-comparison.htm)
は GenAI Applications を **Scale to zero: Yes / "Automatic scaling from 0 to many instances"**
（serverless-style deployment）と明記しており、Container Instances / OKE との差別化点として挙げている。

**実機確認（2026-07-29 / us-chicago-1 / DEPLOYTEST）**: `scaling_type=CONCURRENCY`,
`min_replica=0`, `max_replica=2` で `create_hosted_application` が成功し、**ACTIVE 到達後も
`min_replica=0` が保持される**ことを確認（比較のため `min_replica=1` も作成。両方とも削除済み）。
`inbound_auth_config` は必須で、`inbound_auth_config_type` の値は `"IDCS"` ではなく
**`"IDCS_AUTH_CONFIG"`**（`"IDCS"` は 400 `Invalid InboundAuthConfigType`）。

したがって **OpenSearch のような「常設課金だから既定 OFF」という論点は成立しない**。
アイドル時のレプリカ 0 を既定にすれば、使わない利用者に継続課金は発生しない。
残るコスト論点は「初回リクエストのコールドスタート待ち」であり、費用ではなく体験の問題。

### 本当の欠落

1. **エージェントコンテナのイメージが公開されていない**
   `.github/workflows/release.yml` は `jetuse-api` / `jetuse-fn-router` しか push しない（"agent" の出現 0 件）。
   `packages/agent-containers/build.sh` はローカル podman ビルド専用で、push 先の定義すら無い。
   → 公開スタックが参照できる `jetuse-agent-{openai,langgraph,adk}` が**どのリージョンにも存在しない**。
2. **ORM スタックが設定を一切配線していない**
   `infra/orm` に `HOSTED_AGENT_*` / `AGENT_*_APP_OCID` の記述が 0 件。
   アプリ側（`jetuse_core/settings.py:75-83`, `hosted_agent.py:86-118`）は受け口が既にあるので、**アプリの改修は原則不要**。
3. **Dynamic Group にホスト型リソースの type が無い**
   `modules/iam` の runtime DG は `computecontainerinstance` / `fnfunc` のみ。
   AGT-04 の記録どおり `generativeaihostedapplication` / `generativeaihosteddeployment` の追加が要る
   （コンテナ自身が GenAI 推論を呼ぶ: `agent_common.py` が `inference.generativeai.{region}.../openai/v1` を叩く）。
4. **`enable_auth=false` だと Identity Domain が無い**
   OAuth の発行元が消えるため、この構成ではホスト型エージェントは成立しない。依存を明示する必要がある。

## 前提（依存タスク / 人間の事前作業）

- ~~ADR が先~~ **ADR-0019 承認済み**（2026-07-29）。実装に進んでよい。
- **OCIR リポジトリ（人間作業）**。2026-07-29 時点の実測:

  | リージョン | `jetuse-agent-{openai,langgraph,adk}` |
  |---|---|
  | ap-osaka-1 (kix) | ✅ 作成済み・public（2026-07-29 確認） |
  | us-chicago-1 (ord) | ✅ 作成済み・public（2026-07-29 確認） |
  | ap-tokyo-1 (nrt) / us-ashburn-1 (iad) | 未作成（**本タスクの対象外**） |

  **人間の事前作業は完了済み。着手をブロックするものは無い。**
  push 先は GenAI 実証済みリージョン（kix / ord）に限定する（下記「push 先の範囲」）。
  nrt/iad でも動かす方針に変える場合のみ、リポジトリ6個の追加作成が必要になる。

## 設計判断（ADR-0019 で決定済み）

`docs/decisions/ADR-0019-hosted-agents-in-public-stack.md`（2026-07-29 ユーザー決定）。

| 論点 | 決定 |
|---|---|
| 既定で配備する SDK | **3SDK すべて**（openai_agents / langgraph / adk）。ゼロスケールで費用論点が消えたため絞る理由が無く、SDK切替デモが既定構成で成立する |
| `scaling_config` の既定 | **`min_replica=0`**。デモ用に `hosted_agent_min_replica`（既定 0）で上げられるようにする |
| コールドスタート | 実測して扱いを決める。`hosted_agent.py` の httpx timeout は現行180秒。不足なら延長し、UI 表示の要否も実測値で判断 |
| `enable_auth=false` | 配備しない（OAuth 発行元が無い）。health / UI で理由付きの縮退を出す |

実装時に**実測して再検討する**もの: テナンシの service limit 消費 / apply 時間の伸び。

## 作業内容

1. ~~ADR 起票~~ **完了**: `docs/decisions/ADR-0019-hosted-agents-in-public-stack.md`（承認済み）。
2. **イメージの公開**: `release.yml` に 3SDK の build/push を追加。

   **push 先の範囲**: `jetuse-api` / `jetuse-fn-router` は4リージョンだが、**エージェント画像は
   GenAI 実証済みの kix / ord のみ**とする。ホスト型エージェントはコンテナ自身が GenAI 推論を
   呼ぶため、nrt / iad では配備できても機能しない（`region_guard` が
   `allow_unvalidated_genai_region` の明示オプトインを要求している領域）。
   → その2リージョン以外では hosted-agent モジュールを作らない条件分岐が要る。
   nrt/iad でも動かす方針に変えるなら、リポジトリ6個の追加作成から必要になる。

   `packages/agent-containers/build.sh` と Containerfile を流用し、タグ運用は `jetuse-api` に合わせる
   （`latest` + `${GITHUB_SHA}`）。images ジョブが失敗したら zip も公開しない既存の依存関係は維持する。
3. **Terraform モジュール新設**: `infra/terraform/modules/hosted-agent/`
   （`scaling_config` は **min_replica=0 を既定**にする。ゼロスケールが公開スタックの費用前提）
   - `oci_identity_domains_app`（confidential + OAuth resource。audience/scope、client_credentials のみ）
   - `oci_generative_ai_hosted_application`（`inbound_auth_config.idcs_config` に上記 audience/domain/scope）
   - `oci_generative_ai_hosted_deployment`（`active_artifact.container_uri` = デプロイリージョンの OCIR）
   - SDK ごとに `for_each`。**既定は3SDKすべて**（ADR-0019）。個別に外せる余地は残す
4. **IAM**: `modules/iam` の runtime DG matching rule に
   `generativeaihostedapplication` / `generativeaihosteddeployment` を追加。
   コンテナが GenAI を呼ぶため runtime policy の適用範囲に入ることを確認する
   （**FIX-58 で判明した `generative-ai-response` / `-conversation` と同様に、ホスト型固有の
     resource-type が別途要るかを policy 1文で実在判定してから足す**）。
5. **配線**: `infra/orm/locals.tf` の `api_environment` に
   `HOSTED_AGENT_IDCS_DOMAIN` / `HOSTED_AGENT_CLIENT_ID` / `HOSTED_AGENT_CLIENT_SECRET` /
   `HOSTED_AGENT_SCOPE` と `AGENT_OPENAI_APP_OCID` / `AGENT_LANGGRAPH_APP_OCID` / `AGENT_ADK_APP_OCID` を追加。
   `client_secret` は sensitive として扱い、**出力にも state 外にも平文で出さない**。
6. **縮退の明確化（PORT-02 方針の踏襲）**: 無効化構成では
   `GET /api/health` の `capabilities.agents` に「このスタックではホスト型エージェント未配備」を
   ヒント付きで出し、UI も生の `agent container not configured` ではなく理由を出す。
7. **ドキュメント**: `docs/setup/hosted-agent-oauth.md` を「手動手順の記録（dev 環境・当時）」と明示し、
   公開スタックでは Terraform が自動化する旨へ更新。`docs/setup/orm.md` に新変数を追記。
   `docs/cost-estimate.md` に反映（**ゼロスケールのためアイドル課金なし**・従量のみ）。

## E2E シナリオ（実機・`ops/e2e/README.md` のハーネスを使う）

`loop-config.yml` の `e2e.min_scenarios=2` に対し、本タスクは最低4本実施する。
証跡は `runs/<run-id>/e2e/`（**URL / OCID / パスワードはマスクしてから置く**）。

1. **既存38項目の非回帰**: `node ops/e2e/public-deploy.mjs` を通し **38/38 PASS・4xx/5xx 0件**。
   ホスト型エージェント追加でチャットや RAG が壊れていないことを担保する。
2. **3SDK のエージェント実行**: `/agents` でエージェントを作成し、`openai_agents` /
   `langgraph` / `adk` を切り替えて実行。それぞれ**ツール実行を伴う応答**が返り、
   `agent container not configured` が出ないこと。
3. **コールドスタート実測**: `min_replica=0` の状態から初回 invoke までの所要時間を計測し、
   `hosted_agent.py` の httpx timeout（現行180秒）で足りるかを判定。値を証跡に残す。
4. **無効化構成の縮退**: `enable_auth=false`（または hosted agent 無効）でデプロイし、
   `GET /api/health` の `capabilities.agents` が理由付きの `unavailable` になり、
   UI も生のエラー文字列を出さないこと。
5. **destroy**: `Destroy complete!` で完了し、Hosted Application / Deployment が残らないこと。

## 引き継ぎメモ（新しいコンテキストで着手する人へ）

- **検証テナンシ**: OCI CLI プロファイル `DEPLOYTEST`、コンパートメント `jetuse-test`。
  OCID はコミットしない。取得は
  `oci iam compartment list --profile DEPLOYTEST --all --compartment-id-in-subtree true --query "data[?name=='jetuse-test'].id | [0]" --raw-output`。
  ホームリージョンは `ca-toronto-1`、デプロイ先は `us-chicago-1`。
- **公開スタックの検証手順**は `docs/verification/PUBLIC-DEPLOY-E2E.md` の「再現手順」節が正本
  （配布zipをローカルで作り、ORM Stack を作って apply → E2E → destroy）。
- **イメージビルドは dev インスタンスへ委譲するのが速い**。ローカル macOS は arm64 で、
  linux/amd64 を QEMU エミュレーションでビルドすると **1イメージ約20分**かかる（FIX-58 実測）。
  エージェント画像は3つ増えるため、`dispatch-remote` スキルで x86 の `dev` インスタンスへ
  投げることを検討する。
- **E2E ハーネス**は `ops/e2e/`（FIX-58 で作成・38項目）。ハマりどころは同 README に記載。
- **OCI CLI の落とし穴**（FIX-58 / 本タスク調査で判明。`docs/tips.md` にも記載）:
  `inbound_auth_config_type` は `"IDCS_AUTH_CODE"` ではなく **`"IDCS_AUTH_CONFIG"`**。
  非対話環境では書き込み系コマンドに `--force` が要るが、`identity-domains app patch` には
  `--force` が**無い**ので `echo y |` を使う。

## 完了条件（実機確認の方法を明記）

1. **クリーンなテナンシ**（FIX-58 の検証と同じ DEPLOYTEST / `jetuse-test`）へ公開 zip 相当でデプロイし、
   `enable_hosted_agents` 既定のまま **手動作業ゼロ**で apply が成功する。
2. ログイン後 `/agents` でエージェントを作成し、チャットで実行して**ツール実行を伴う応答が返る**
   （`agent container not configured` が出ない）。**3SDK すべてで切替して確認**する。
3. `GET /api/health` の `capabilities.agents` が `ok`。無効化構成では `unavailable` + 理由が出る。
   また **`min_replica=0` からのコールドスタート時間を実測**し、`hosted_agent.py` の
   timeout（現行180秒）で足りることを確認する。
4. **destroy が成功する**（FIX-58 で入れたバケット掃除と同様、ホスト型リソースが残らないこと）。
5. `make lint && make test`、`terraform validate`（orm/dev）、`terraform test`（iam）が緑。
6. 上記を `docs/verification/PORT-03.md` に記録（実行ログ・OCID はマスク）。

## 成果物

- ADR: `docs/decisions/ADR-0019-hosted-agents-in-public-stack.md`（**作成済み**）
- コード: `.github/workflows/release.yml` / `infra/terraform/modules/hosted-agent/` /
  `modules/iam` / `infra/orm/{main,locals,variables}.tf` / `schema.yaml` /
  必要なら `packages/api/jetuse_core/health.py`（agents capability）
- 検証: `docs/verification/PORT-03.md`
- ドキュメント更新: `docs/setup/hosted-agent-oauth.md` / `docs/setup/orm.md` / `docs/cost-estimate.md` /
  `docs/tips.md`（実機で分かったこと）

## 禁止事項

- **`client_secret` を含む認証情報をコミットしない**。Terraform 出力にも平文で出さない。
- テナンシ/コンパートメント OCID の実値をコミットしない。
- 検証は **dev コンパートメント or 専用テストコンパートメント限定**。既存リソース（VCN `develop` /
  インスタンス `dev` / バケット `jetuse-oci-source-documents`）に触れない。
- スパイク用リソースは `jetuse-spike-` プレフィックスを付ける。
