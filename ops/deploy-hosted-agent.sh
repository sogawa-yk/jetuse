#!/usr/bin/env bash
# AGT-04: LangGraphサンプルエージェントをOCIホスト型アプリケーションへデプロイする。
# 前提: docs/setup/iam.md「AGT-04」節のIAM整備済み / OCIRログイン済み / .envにCOMPARTMENT_OCID
# 使い方: ops/deploy-hosted-agent.sh [タグ]   (既定: 0.1.0)
set -euo pipefail
cd "$(dirname "$0")/.."

TAG="${1:-0.1.0}"
REPO=kix.ocir.io/idqcucnenh88/jetuse-spike-hosted-agent
COMP=$(grep '^COMPARTMENT_OCID=' .env | cut -d= -f2)
DOMAIN=https://idcs-1a7db50d84bd47acb4ef51b5bcbdf56f.identity.oraclecloud.com

echo "== build & push ${REPO}:${TAG}"
podman build -t "${REPO}:${TAG}" packages/hosted-agent-sample
podman push "${REPO}:${TAG}"

echo "== create hosted application"
APP=$(oci generative-ai hosted-application create \
  --display-name jetuse-spike-hosted-agent \
  --compartment-id "$COMP" \
  --description "AGT-04: LangGraph sample agent" \
  --scaling-config '{"scalingType":"CONCURRENCY","minReplica":1,"maxReplica":1,"targetConcurrencyThreshold":10}' \
  --inbound-auth-config '{"inboundAuthConfigType":"IDCS_AUTH_CONFIG","idcsConfig":{"domainUrl":"'"$DOMAIN"'","audience":"jetuse-spike-agent","scope":"invoke"}}' \
  --environment-variables '[
    {"name":"COMPARTMENT_OCID","type":"PLAINTEXT","value":"'"$COMP"'"},
    {"name":"AUTH_MODE","type":"PLAINTEXT","value":"resource_principal"},
    {"name":"OCI_REGION","type":"PLAINTEXT","value":"ap-osaka-1"}]' \
  --query 'data.id' --raw-output)
echo "APP=$APP"

until [ "$(oci generative-ai hosted-application get --hosted-application-id "$APP" \
        --query 'data."lifecycle-state"' --raw-output)" = ACTIVE ]; do sleep 15; done

echo "== create hosted deployment (image pull + 脆弱性スキャン)"
# 注意: 1アプリ=1デプロイメント。既存があると "already exists"（DELETING中も同様）。
# 削除完了はGETの404ではなく lifecycle-state=DELETED で判定する（DELETED後もGETは200を返す）
DEP=$(oci generative-ai hosted-deployment create \
  --display-name jetuse-spike-hosted-agent-dep \
  --compartment-id "$COMP" \
  --hosted-application-id "$APP" \
  --active-artifact '{"artifactType":"SIMPLE_DOCKER_ARTIFACT","containerUri":"'"$REPO"'","tag":"'"$TAG"'"}' \
  --query 'data.id' --raw-output)
echo "DEP=$DEP"

# lifecycle-stateはCLI未知のenum(NEEDS_ATTENTION等)を返すことがあるためraw-requestで監視
while :; do
  ST=$(oci raw-request --http-method GET \
    --target-uri "https://generativeai.ap-osaka-1.oci.oraclecloud.com/20231130/hostedDeployments/$DEP" \
    | python3 -c "import sys,json;d=json.load(sys.stdin)['data'];print(d['lifecycleState'], (d.get('activeArtifact') or {}).get('status'), (d.get('artifacts') or [{}])[0].get('status'))")
  echo "$(date +%H:%M:%S) $ST"
  case "$ST" in
    ACTIVE*ACTIVE*) break ;;
    *FAILED*|NEEDS_ATTENTION*) echo "デプロイ失敗。work-requestのエラーを確認してください" >&2; exit 1 ;;
  esac
  sleep 20
done

# invoke URL形式（2026-06-12実機確定。リソースJSONにendpointフィールドは無くURLは規則ベース）:
#   https://inference.generativeai.ap-osaka-1.oci.oraclecloud.com/20251112/hostedApplications/{APP}/actions/invoke/{コンテナ側パス}
# 認証: IDCSのBearer（aud=jetuse-spike-agent / scope=invoke のclient_credentialsトークン）
BASE="https://inference.generativeai.ap-osaka-1.oci.oraclecloud.com/20251112/hostedApplications/$APP/actions/invoke"
echo "DEPLOY_OK"
echo "invoke例:"
echo "  TOK=\$(curl -s -u '<client_id>:<client_secret>' -d 'grant_type=client_credentials&scope=jetuse-spike-agentinvoke' $DOMAIN/oauth2/v1/token | jq -r .access_token)"
echo "  curl -H \"Authorization: Bearer \$TOK\" $BASE/health"
echo "  curl -X POST -H \"Authorization: Bearer \$TOK\" -H 'Content-Type: application/json' -d '{\"input\":\"...\"}' $BASE/invoke"
