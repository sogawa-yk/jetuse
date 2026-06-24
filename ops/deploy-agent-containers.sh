#!/usr/bin/env bash
# AGT-MULTI(ADR-0009): 3SDK汎用ReActコンテナをOCI Hosted Applicationへデプロイ。
# 前提: OCIRログイン済み / 3イメージpush済み / jetuse-dg(Defaultドメイン)にhostedリソース型追加済み(GAP-04)
#       / IDCS OAuthアプリ jetuse-agent(audience=jetuse-agent, scope=invoke)作成済み(GAP-04)
# 使い方: ops/deploy-agent-containers.sh [タグ]   (既定: 0.1.0)
# 出力: 各APPのOCID(tfvarsの AGENT_*_APP_OCID に設定する)
set -euo pipefail
cd "$(dirname "$0")/.."

TAG="${1:-0.1.0}"
TFV=infra/terraform/environments/dev/terraform.tfvars
ev() { grep "^$1=" .env | cut -d= -f2- || true; }
# ADB/SemanticStoreの機密はtfvarsのapi_environmentから取得(非コミット)
tfv() { grep -E "^  $1 *=" "$TFV" | head -1 | sed -E 's/.*= *"(.*)"/\1/' || true; }
COMP=$(ev COMPARTMENT_OCID)
PROJECT=$(ev PROJECT_OCID)
DOMAIN=https://idcs-1a7db50d84bd47acb4ef51b5bcbdf56f.identity.oraclecloud.com
REGION=ap-osaka-1
NS=idqcucnenh88
SEMSTORE=$(tfv SEMSTORE_OCID)
ADB_DSN=$(tfv ADB_DSN)
ADB_QPW=$(tfv ADB_QUERY_PASSWORD)
ADB_WPW=$(tfv ADB_WALLET_PASSWORD)
ADB_BUCKET=$(tfv ADB_WALLET_BUCKET)
ADB_OBJ=$(tfv ADB_WALLET_OBJECT)

ENVVARS='[
  {"name":"COMPARTMENT_OCID","type":"PLAINTEXT","value":"'"$COMP"'"},
  {"name":"PROJECT_OCID","type":"PLAINTEXT","value":"'"$PROJECT"'"},
  {"name":"AUTH_MODE","type":"PLAINTEXT","value":"resource_principal"},
  {"name":"OCI_REGION","type":"PLAINTEXT","value":"'"$REGION"'"},
  {"name":"SEMSTORE_OCID","type":"PLAINTEXT","value":"'"$SEMSTORE"'"},
  {"name":"ADB_DSN","type":"PLAINTEXT","value":"'"$ADB_DSN"'"},
  {"name":"ADB_QUERY_PASSWORD","type":"PLAINTEXT","value":"'"$ADB_QPW"'"},
  {"name":"ADB_WALLET_PASSWORD","type":"PLAINTEXT","value":"'"$ADB_WPW"'"},
  {"name":"ADB_WALLET_BUCKET","type":"PLAINTEXT","value":"'"$ADB_BUCKET"'"},
  {"name":"ADB_WALLET_OBJECT","type":"PLAINTEXT","value":"'"${ADB_OBJ:-adb_wallet.zip}"'"}]'

INBOUND='{"inboundAuthConfigType":"IDCS_AUTH_CONFIG","idcsConfig":{"domainUrl":"'"$DOMAIN"'","audience":"jetuse-agent","scope":"invoke"}}'
SCALING='{"scalingType":"CONCURRENCY","minReplica":1,"maxReplica":1,"targetConcurrencyThreshold":10}'

deploy_one() {
  local sdk="$1" repo="jetuse-dev-agent-$1" name="jetuse-dev-agent-$1"
  echo "==== [$sdk] create hosted application ===="
  local app
  app=$(oci generative-ai hosted-application create \
    --display-name "$name" --compartment-id "$COMP" \
    --description "AGT-MULTI $sdk ReAct agent (ADR-0009)" \
    --scaling-config "$SCALING" --inbound-auth-config "$INBOUND" \
    --environment-variables "$ENVVARS" --query 'data.id' --raw-output)
  echo "[$sdk] APP=$app"
  until [ "$(oci generative-ai hosted-application get --hosted-application-id "$app" \
          --query 'data."lifecycle-state"' --raw-output)" = ACTIVE ]; do sleep 12; done
  echo "==== [$sdk] create hosted deployment (image pull) ===="
  local dep
  dep=$(oci generative-ai hosted-deployment create \
    --display-name "$name-dep" --compartment-id "$COMP" --hosted-application-id "$app" \
    --active-artifact '{"artifactType":"SIMPLE_DOCKER_ARTIFACT","containerUri":"kix.ocir.io/'"$NS"'/'"$repo"'","tag":"'"$TAG"'"}' \
    --query 'data.id' --raw-output)
  echo "[$sdk] DEP=$dep"
  while :; do
    st=$(oci raw-request --http-method GET \
      --target-uri "https://generativeai.$REGION.oci.oraclecloud.com/20231130/hostedDeployments/$dep" \
      | python3 -c "import sys,json;d=json.load(sys.stdin)['data'];print(d['lifecycleState'],(d.get('activeArtifact') or {}).get('status'))")
    echo "$(date +%H:%M:%S) [$sdk] $st"
    case "$st" in
      ACTIVE*ACTIVE*) break ;;
      *FAILED*|NEEDS_ATTENTION*) echo "[$sdk] デプロイ失敗" >&2; return 1 ;;
    esac
    sleep 20
  done
  echo "RESULT $sdk APP_OCID=$app"
}

for sdk in "${@:2}"; do deploy_one "$sdk"; done
# 引数2以降が無ければ3つ全部
if [ "$#" -le 1 ]; then for sdk in openai langgraph adk; do deploy_one "$sdk"; done; fi
