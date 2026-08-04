#!/usr/bin/env bash
# AGT-04: LangGraphサンプルエージェントをOCIホスト型アプリケーションへデプロイする。
# 前提: docs/setup/iam.md「AGT-04」節のIAM整備済み / OCIRログイン済み / .envにCOMPARTMENT_OCID
# 使い方: ops/deploy-hosted-agent.sh [タグ]   (既定: 0.1.0)
set -euo pipefail
cd "$(dirname "$0")/.."

TAG="${1:-0.1.0}"
. "$(dirname "$0")/_region.sh"
REGION=$(jetuse_region)
jetuse_use_cli_region "$REGION"   # oci CLI を必ずこのリージョンへ向ける
# OCIR の名前空間はテナンシ固有なので**リポジトリに埋めない**（規約）。
# Object Storage の名前空間と同じ値なので、.env の OS_NAMESPACE を既定に使う。
# **.env は source していない**ので、環境変数ではなくファイルから読む。
_ns() { grep "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"\r'; }
NS="${OCIR_NAMESPACE:-$(_ns OCIR_NAMESPACE)}"
[ -n "$NS" ] || NS="$(_ns OS_NAMESPACE)"
# どちらも無いなら止める（誤ったテナンシの repository を掴まないため）
[ -n "$NS" ] || { echo "OCIR_NAMESPACE か OS_NAMESPACE を .env に設定してください" >&2; exit 1; }
REPO=$(jetuse_ocir_host "$REGION")/$NS/jetuse-spike-hosted-agent
COMP=$(grep '^COMPARTMENT_OCID=' .env | cut -d= -f2)
# Identity Domain の URL はテナンシ固有なので**リポジトリに埋めない**（規約）。
# .env の IDENTITY_DOMAIN_URL から読む。未設定なら止める（誤った認証ドメインを掴まないため）。
DOMAIN="${IDENTITY_DOMAIN_URL:-$(_ns IDENTITY_DOMAIN_URL)}"
[ -n "$DOMAIN" ] || { echo "IDENTITY_DOMAIN_URL を .env に設定してください" >&2; exit 1; }

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
    {"name":"OCI_REGION","type":"PLAINTEXT","value":"'"$REGION"'"}]' \
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
    --target-uri "https://generativeai.$REGION.oci.oraclecloud.com/20231130/hostedDeployments/$DEP" \
    | python3 -c "import sys,json;d=json.load(sys.stdin)['data'];print(d['lifecycleState'], (d.get('activeArtifact') or {}).get('status'), (d.get('artifacts') or [{}])[0].get('status'))")
  echo "$(date +%H:%M:%S) $ST"
  case "$ST" in
    ACTIVE*ACTIVE*) break ;;
    *FAILED*|NEEDS_ATTENTION*) echo "デプロイ失敗。work-requestのエラーを確認してください" >&2; exit 1 ;;
  esac
  sleep 20
done

# invoke URL形式（2026-06-12実機確定。リソースJSONにendpointフィールドは無くURLは規則ベース）:
#   https://inference.generativeai.{REGION}.oci.oraclecloud.com/20251112/hostedApplications/{APP}/actions/invoke/{コンテナ側パス}
# 認証: IDCSのBearer（aud=jetuse-spike-agent / scope=invoke のclient_credentialsトークン）
BASE="https://inference.generativeai.$REGION.oci.oraclecloud.com/20251112/hostedApplications/$APP/actions/invoke"
echo "DEPLOY_OK"
echo "invoke例:"
echo "  TOK=\$(curl -s -u '<client_id>:<client_secret>' -d 'grant_type=client_credentials&scope=jetuse-spike-agentinvoke' $DOMAIN/oauth2/v1/token | jq -r .access_token)"
echo "  curl -H \"Authorization: Bearer \$TOK\" $BASE/health"
echo "  curl -X POST -H \"Authorization: Bearer \$TOK\" -H 'Content-Type: application/json' -d '{\"input\":\"...\"}' $BASE/invoke"
