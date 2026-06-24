#!/usr/bin/env bash
# AGT-MULTI: 既存3 Hosted Application を削除(=デプロイメントもカスケード削除)し、
# 指定タグ+フルenv(ADB含む)で作り直す。新APP OCIDを RESULT 行で出力する。
# 既存アクティブdeploymentは直接削除できないが、アプリ削除でカスケードされる(実機確定)。
set -uo pipefail
cd "$(dirname "$0")/.."
TAG="${1:?tag required}"
TFV=infra/terraform/environments/dev/terraform.tfvars
COMP=$(grep '^COMPARTMENT_OCID=' .env | cut -d= -f2)
tfvocid() { grep -E "^  $1 *=" "$TFV" | head -1 | sed -E 's/.*= *"(.*)"/\1/'; }

echo "==== delete existing 3 apps (cascade deployments) ===="
for sdk in OPENAI LANGGRAPH ADK; do
  app=$(tfvocid "AGENT_${sdk}_APP_OCID")
  [ -z "$app" ] && { echo "[$sdk] no app ocid in tfvars"; continue; }
  echo "[$sdk] delete $app"
  oci generative-ai hosted-application delete --hosted-application-id "$app" --force >/dev/null 2>&1 || true
  for i in $(seq 1 40); do
    st=$(oci generative-ai hosted-application get --hosted-application-id "$app" \
         --query 'data."lifecycle-state"' --raw-output 2>/dev/null || echo GONE)
    [ "$st" = DELETED ] || [ "$st" = GONE ] && { echo "[$sdk] $st"; break; }
    sleep 12
  done
done

echo "==== recreate 3 apps with $TAG (full env) ===="
exec bash ops/deploy-agent-containers.sh "$TAG"
