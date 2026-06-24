#!/usr/bin/env bash
# AGT-MULTI: 既存3 Hosted Application の env を更新(ADB/SemanticStore追加)し、
# 指定タグの新イメージでデプロイメントを作り直す(APP OCIDは不変)。
# 使い方: ops/redeploy-agent-env.sh <タグ>
set -euo pipefail
cd "$(dirname "$0")/.."
TAG="${1:?tag required}"
TFV=infra/terraform/environments/dev/terraform.tfvars
ev() { grep "^$1=" .env | cut -d= -f2- || true; }
tfv() { grep -E "^  $1 *=" "$TFV" | head -1 | sed -E 's/.*= *"(.*)"/\1/' || true; }
REGION=ap-osaka-1; NS=idqcucnenh88
COMP=$(ev COMPARTMENT_OCID)
ADB_OBJ=$(tfv ADB_WALLET_OBJECT); ADB_OBJ=${ADB_OBJ:-adb_wallet.zip}
ENVVARS='[
  {"name":"COMPARTMENT_OCID","type":"PLAINTEXT","value":"'"$COMP"'"},
  {"name":"PROJECT_OCID","type":"PLAINTEXT","value":"'"$(ev PROJECT_OCID)"'"},
  {"name":"AUTH_MODE","type":"PLAINTEXT","value":"resource_principal"},
  {"name":"OCI_REGION","type":"PLAINTEXT","value":"'"$REGION"'"},
  {"name":"SEMSTORE_OCID","type":"PLAINTEXT","value":"'"$(tfv SEMSTORE_OCID)"'"},
  {"name":"ADB_DSN","type":"PLAINTEXT","value":"'"$(tfv ADB_DSN)"'"},
  {"name":"ADB_QUERY_PASSWORD","type":"PLAINTEXT","value":"'"$(tfv ADB_QUERY_PASSWORD)"'"},
  {"name":"ADB_WALLET_PASSWORD","type":"PLAINTEXT","value":"'"$(tfv ADB_WALLET_PASSWORD)"'"},
  {"name":"ADB_WALLET_BUCKET","type":"PLAINTEXT","value":"'"$(tfv ADB_WALLET_BUCKET)"'"},
  {"name":"ADB_WALLET_OBJECT","type":"PLAINTEXT","value":"'"$ADB_OBJ"'"}]'

one() {
  local sdk="$1" repo="jetuse-dev-agent-$1"
  local app; app=$(tfv "AGENT_$(echo "$sdk" | tr a-z A-Z)_APP_OCID")
  echo "==== [$sdk] update env: $app ===="
  oci generative-ai hosted-application update --hosted-application-id "$app" \
    --environment-variables "$ENVVARS" --force >/dev/null
  echo "[$sdk] wait application ACTIVE (env update settles)"
  until [ "$(oci generative-ai hosted-application get --hosted-application-id "$app" \
          --query 'data."lifecycle-state"' --raw-output 2>/dev/null)" = ACTIVE ]; do sleep 10; done
  echo "[$sdk] update deployment active-artifact -> $TAG (in place)"
  local dep; dep=$(oci generative-ai hosted-deployment-collection list-hosted-deployments \
    --compartment-id "$COMP" --region "$REGION" --all \
    --query "data.items[?\"hosted-application-id\"=='$app' && \"lifecycle-state\"!='DELETED'].id | [0]" --raw-output)
  [ -z "$dep" ] && { echo "[$sdk] no deployment" >&2; return 1; }
  oci generative-ai hosted-deployment update --hosted-deployment-id "$dep" \
    --active-artifact '{"artifactType":"SIMPLE_DOCKER_ARTIFACT","containerUri":"kix.ocir.io/'"$NS"'/'"$repo"'","tag":"'"$TAG"'"}' \
    --force >/dev/null
  sleep 10
  while :; do
    st=$(oci raw-request --http-method GET \
      --target-uri "https://generativeai.$REGION.oci.oraclecloud.com/20231130/hostedDeployments/$dep" \
      | python3 -c "import sys,json;d=json.load(sys.stdin)['data'];print(d['lifecycleState'],(d.get('activeArtifact') or {}).get('status'),(d.get('activeArtifact') or {}).get('tag'))")
    echo "$(date +%H:%M:%S) [$sdk] $st"
    case "$st" in ACTIVE\ ACTIVE\ "$TAG") break;; *FAILED*|NEEDS_ATTENTION*) echo "[$sdk] FAILED" >&2; return 1;; esac
    sleep 20
  done
  echo "[$sdk] OK"
}
for sdk in openai langgraph adk; do one "$sdk"; done
echo "ALL DONE"
