# AGT-04 検証レポート — Applications/Deployments（ホスト型エージェント）

日付: 2026-06-12 / ブランチ: task/agt-04
結論: **E2E完全成功**。LangGraphサンプルエージェントをホスト型デプロイし、IDCS OAuth経由のinvokeで実応答を確認（6.3秒、ローカル実行と同速）。

## 検証したもの

LangGraph製サンプルエージェント（`packages/hosted-agent-sample/`）を題材に、
OCI Enterprise AIの **Hosted Applications / Hosted Deployments**（コンテナ持ち込みでエージェントをマネージドホスティングする機能）を
デプロイ手順からinvoke実測まで実機で通した。

- グラフ: research→summarize の2ノード。LLMは OpenAI互換エンドポイントの gpt-oss-120b（IAM署名は `oci-genai-auth`、
  ホスト環境では **`AUTH_MODE=resource_principal` が実際に機能**＝ホスト型アプリ自身のリソースプリンシパルでLLM呼び出し成功）
- HTTP契約はアプリ側で自由（本サンプル: `POST /invoke` / `GET /health`、**port 8080で動作確認**）

## 実機確定事項

| 項目 | 結果 |
|---|---|
| ローカル動作 | `/invoke` 6.3秒で2ノード完走 |
| OCIRプッシュ | 新規リポジトリの自動作成は403 → `oci artifacts container repository create` で明示作成後に成功 |
| application作成 | 必須は display-name / compartment-id / **inbound-auth-config**（省略すると "inboundAuthConfig must not be null"）。専用クラスタ等の高額前提パラメータは**無し**（replica数ベース、スパイクはmin/max 1） |
| inbound-auth | **IDCS_AUTH_CONFIG一択**（SDK enum）。domainUrl/audience/scope を指定 |
| 環境変数 | `type` は `PLAINTEXT` / `VAULT`（CLIの生成例 `PLAIN_TEXT` は誤り） |
| application | 約90秒でACTIVE。endpoint-mode PUBLIC / outbound MANAGED が既定 |
| deployment | `--compartment-id` 必須（CLI helpでは読み取りにくい）。**1アプリ=1デプロイメント**（既存があると "already exists"、DELETING中も同様）。削除完了はGET 404ではなく **lifecycle-state=DELETED** で判定。**ACTIVEなデプロイメントは直接削除不可**（application削除でカスケード） |
| IAM | 動的グループ2タイプ + `read repos` が無いと artifact FAILED（"container image could not be accessed or validated"）。**ポリシー文だけでは不可**（動的グループルール必須）。**反映に5〜10分**（追加直後の再試行は同エラーで失敗、8分後に成功） |
| invoke認証 | IDCSの client_credentials トークンで成功。アプリ設定の audience/scope とトークンの `aud`/`scope` クレームが一致する必要。スパイクでは jetuse-dev-domain に資源登録+クライアント兼用アプリを1つ作成（fqs=`jetuse-spike-agentinvoke`、audience末尾セパレータ無しでも分解された） |
| **invoke URL（未文書）** | `https://inference.generativeai.{region}.oci.oraclecloud.com/20251112/hostedApplications/{APP_OCID}/actions/invoke/{コンテナ側パス}`。リソースJSONにendpointフィールドは存在せず、PE用ドキュメントのFQDN例から規則を推定→`/actions/invoke` 配下がコンテナへパススルーされることを実測で確定 |
| E2E実測 | `/health` 200 / `/invoke` 200・6.3秒（ホスト型でもローカルと同速。コールドスタート遅延は観測されず） |

## 必要なIAM（適用済み — docs/setup/iam.md「AGT-04」節）

1. `jetuse-dg` に `resource.type='generativeaihostedapplication'` / `'generativeaihosteddeployment'`（jetuse-proto限定）を追加
2. `allow dynamic-group jetuse-dg to read repos in compartment jetuse-proto`

LLM呼び出し（リソースプリンシパル）は既存の `use generative-ai-family` でカバー。

## 後片付け

スパイクの hosted-deployment / hosted-application / IDCSアプリ（jetuse-spike-agent-client）/ OCIRリポジトリ をすべて削除済み。
再現は `ops/deploy-hosted-agent.sh` 一発（+ IDCSのトークン発行用アプリ作成）。

## アプリ統合の位置づけ（Phase 9へ）

ホスト型エージェントは「OCI側にエージェント本体を置く」形態で、本アプリのスクラッチReActエージェント（AGT-01〜03）とは別系統。
アプリからの「インポート」は invoke endpoint をツール/エージェントとして呼ぶ薄いクライアントになる。
エージェントフレームワーク対応（Phase 9: OpenAI Agents SDK→LangGraph）の実行基盤としてこの機構を本格利用する。
