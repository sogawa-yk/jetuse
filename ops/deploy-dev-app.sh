#!/usr/bin/env bash
# SP3-07: jetuse:dev プレビュー(RM スタック jetuse-dev-app)への統合デプロイ。
# GitHub Actions (.github/workflows/deploy-dev.yml) の各 step は本スクリプトのサブコマンドを
# そのまま呼ぶ(= ローカル検証とワークフローが同一手順)。RM の apply/plan は
# `oci resource-manager job` 経由(ローカル terraform apply は権限層で遮断される — SP2 実績)。
#
# 使い方:
#   ops/deploy-dev-app.sh all          # image → update-stack → apply → spa → smoke
#   ops/deploy-dev-app.sh <subcommand> # 個別実行(冪等)
#   ops/deploy-dev-app.sh seed-env     # ORASEJAPAN 材料+RAG_BUCKET/APP_SESSION_SECRET を
#                                      # RM 変数へ一度だけシード(ローカル。CI には置かない)
#
# 前提: OCI CLI 認証(ローカル=~/.oci/config、CI=OCI_CLI_* env)。COMPARTMENT_OCID(.env or env)。
# 共有スタック 1 本のため apply は直列(先行 RM job の完了を待つ)。destroy は扱わない(禁止)。
set -euo pipefail
cd "$(dirname "$0")/.."

# ローカルは .env、CI は env/secrets(存在しなければスキップ)
if [ -f .env ]; then set -a; . ./.env; set +a; fi

: "${COMPARTMENT_OCID:?COMPARTMENT_OCID is required (.env or env)}"
STACK_NAME="${STACK_NAME:-jetuse-dev-app}"
CONTAINER_TOOL="${CONTAINER_TOOL:-$(command -v podman >/dev/null 2>&1 && echo podman || echo docker)}"
TF_WORKDIR="infra/terraform/environments/dev-app"

BRANCH="${GITHUB_REF_NAME:-$(git branch --show-current)}"
SHORT_SHA="$(git rev-parse --short HEAD)"
TAG="$(echo "${BRANCH}" | tr '/' '-' | tr -cd 'a-zA-Z0-9._-')-${SHORT_SHA}"

# 一時ファイルはまとめて EXIT で回収(関数内 RETURN trap は関数返却後も残留し、set -u の下で
# ローカル変数参照が unbound になる — 実バグを踏んだため trap はこの 1 本だけにする)
TMP_ROOT="$(mktemp -d)"
chmod 700 "${TMP_ROOT}"
trap 'rm -rf "${TMP_ROOT}"' EXIT

ns() { oci os ns get --query data --raw-output; }

ocir_host() { # OCIR エンドポイントは region から導出(実値をコミットしない — review-2 m001)
  if [ -z "${OCIR_HOST:-}" ]; then
    local region key
    region="${OCI_CLI_REGION:-${OCI_REGION:?OCI_REGION or OCI_CLI_REGION is required}}"
    key="$(oci iam region list --query "data[?name=='${region}'].key | [0]" --raw-output \
      | tr '[:upper:]' '[:lower:]')"
    [ -n "${key}" ] && [ "${key}" != "null" ] \
      || { echo "cannot derive OCIR host from region '${region}' (set OCIR_HOST)" >&2; exit 1; }
    OCIR_HOST="${key}.ocir.io"
  fi
  echo "${OCIR_HOST}"
}

stack_id() {
  oci resource-manager stack list --compartment-id "${COMPARTMENT_OCID}" \
    --query "data[?\"display-name\"=='${STACK_NAME}'] | [0].id" --raw-output
}

tf_output() { # tf_output <name> — スタックの tf state から output を読む
  oci resource-manager stack get-stack-tf-state --stack-id "$(stack_id)" --file - \
    | jq -r ".outputs[\"$1\"].value"
}

image_url() { echo "$(ocir_host)/$(ns)/jetuse-dev-api:${TAG}"; }

# --- サブコマンド ---

cmd_image() { # API イメージ build → OCIR push(tag = <branch>-<short-sha>)
  local image; image="$(image_url)"
  if [ -n "${OCIR_USERNAME:-}" ] && [ -n "${OCIR_AUTH_TOKEN:-}" ]; then
    echo "${OCIR_AUTH_TOKEN}" | "${CONTAINER_TOOL}" login "$(ocir_host)" \
      -u "${OCIR_USERNAME}" --password-stdin
  fi
  echo "== build & push ${image}"
  "${CONTAINER_TOOL}" build -f packages/api/Containerfile -t "${image}" .
  "${CONTAINER_TOOL}" push "${image}"
}

cmd_spa() { # SPA build → スタックの SPA バケットへ同期
  local bucket; bucket="$(tf_output spa_bucket)"
  [ -n "${bucket}" ] && [ "${bucket}" != "null" ] || { echo "spa_bucket output not found"; exit 1; }
  if [ ! -d packages/web/node_modules ] || [ -n "${CI:-}" ]; then
    npm --prefix packages/web ci
  fi
  npm --prefix packages/web run build
  (cd packages/web && bash scripts/deploy.sh "${bucket}")
}

cmd_update_stack() { # tf 構成 zip + image_url 変数を RM スタックへ反映(他変数はマージで温存)
  local sid tmp; sid="$(stack_id)"; tmp="$(mktemp -d -p "${TMP_ROOT}")"
  # 実行中 RM job がある間の stack update は競合/構成すり替えになる — update も直列化(review-2 M001)
  _wait_no_active_job "${sid}"
  # lock ファイルはローカル validate の副作用で provider 版数を固定してしまうため除外
  (zip -qr "${tmp}/config.zip" infra/terraform \
    -x '*/.terraform/*' -x '*.tfstate' -x '*.tfstate.*' -x '*.tfplan' -x '*.tfvars' \
    -x '*.terraform.lock.hcl')
  oci resource-manager stack get --stack-id "${sid}" --query data.variables > "${tmp}/vars.json"
  jq --arg img "$(image_url)" '. + {image_url: $img}' "${tmp}/vars.json" > "${tmp}/vars.new.json"
  oci resource-manager stack update --stack-id "${sid}" \
    --config-source "${tmp}/config.zip" --working-directory "${TF_WORKDIR}" \
    --variables "file://${tmp}/vars.new.json" --force >/dev/null
  echo "== stack updated: image_url=$(image_url), config=${TF_WORKDIR}"
}

cmd_seed_env() { # 環境依存値・秘匿値を .env / ~/.oci から RM 変数へシード(ローカルで一度だけ。
  # ORASEJAPAN 材料 + RAG_BUCKET / APP_SESSION_SECRET。以降のデプロイは変数マージで温存)
  : "${GEN_SHARED_PROFILE:?GEN_SHARED_PROFILE is required (.env)}"
  : "${GEN_SHARED_COMPARTMENT_OCID:?GEN_SHARED_COMPARTMENT_OCID is required (.env)}"
  : "${RAG_BUCKET:?RAG_BUCKET is required (.env — 生成 SPA バンドル/RAG の保管バケット)}"
  : "${APP_SESSION_SECRET:?APP_SESSION_SECRET is required (.env)}"
  local sid tmp; sid="$(stack_id)"; tmp="$(mktemp -d -p "${TMP_ROOT}")"
  _wait_no_active_job "${sid}"
  # プロファイルの user/fingerprint/tenancy/region/key_file を読み取り(値は表示しない)
  python3 - "$GEN_SHARED_PROFILE" > "${tmp}/prof.json" <<'PY'
import configparser, json, os, sys
cp = configparser.ConfigParser()
cp.read(os.path.expanduser("~/.oci/config"))
p = cp[sys.argv[1]]
print(json.dumps({k: p[k] for k in ("user", "fingerprint", "tenancy", "region", "key_file")}))
PY
  local key_file; key_file="$(jq -r .key_file "${tmp}/prof.json")"
  base64 -w0 "${key_file/#\~/$HOME}" > "${tmp}/key.b64"
  oci resource-manager stack get --stack-id "${sid}" --query data.variables > "${tmp}/vars.json"
  jq --slurpfile prof "${tmp}/prof.json" --rawfile key "${tmp}/key.b64" \
     --arg profile "${GEN_SHARED_PROFILE}" --arg comp "${GEN_SHARED_COMPARTMENT_OCID}" \
     --arg bucket "${RAG_BUCKET}" --arg app_secret "${APP_SESSION_SECRET}" \
     '. + {gen_shared_profile: $profile, gen_shared_compartment_ocid: $comp,
           gen_shared_user_ocid: $prof[0].user, gen_shared_tenancy_ocid: $prof[0].tenancy,
           gen_shared_fingerprint: $prof[0].fingerprint, gen_shared_region: $prof[0].region,
           gen_shared_key_pem_b64: $key,
           rag_bucket: $bucket, app_session_secret: $app_secret}' \
     "${tmp}/vars.json" > "${tmp}/vars.new.json"
  oci resource-manager stack update --stack-id "${sid}" \
    --variables "file://${tmp}/vars.new.json" --force >/dev/null
  echo "== env vars seeded into stack (gen-shared profile=${GEN_SHARED_PROFILE}, rag_bucket, app_session_secret)"
}

_wait_no_active_job() { # 共有スタックにつき apply は直列: 先行 job の完了を待つ(最大30分)
  local sid="$1" i
  for i in $(seq 1 180); do
    local active
    active="$(oci resource-manager job list --stack-id "${sid}" --limit 10 \
      --query "data[?\"lifecycle-state\"=='IN_PROGRESS' || \"lifecycle-state\"=='ACCEPTED'] | length(@)" \
      --raw-output)"
    [ "${active}" = "0" ] && return 0
    echo "== waiting for ${active} active RM job(s)... (${i}/180)"
    sleep 10
  done
  echo "timed out waiting for active RM jobs"; exit 1
}

_run_job() { # _run_job <create-subcmd> <args...> — job 作成→完了待ち→失敗ならログを出して exit 1
  local out jid state
  out="$(oci resource-manager job "$@" \
    --wait-for-state SUCCEEDED --wait-for-state FAILED --wait-for-state CANCELED \
    --max-wait-seconds 3600 --wait-interval-seconds 15)"
  jid="$(echo "${out}" | jq -r .data.id)"
  state="$(echo "${out}" | jq -r '.data."lifecycle-state"')"
  echo "== RM job ${jid}: ${state}" >&2
  if [ "${state}" != "SUCCEEDED" ]; then
    local logs
    logs="$(oci resource-manager job get-job-logs-content --job-id "${jid}" \
      --query data --raw-output 2>/dev/null || true)"
    echo "${logs}" >&2
    if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
      { echo "### RM job ${state}: ${jid}"; echo '```'
        echo "${logs}" | tail -c 8000; echo '```'; } >> "${GITHUB_STEP_SUMMARY}"
    fi
    exit 1
  fi
  echo "${jid}"
}

cmd_apply() { # plan job → apply job(FROM_PLAN_JOB_ID)。直列ガード付き
  local sid plan_jid; sid="$(stack_id)"
  _wait_no_active_job "${sid}"
  plan_jid="$(_run_job create-plan-job --stack-id "${sid}" --display-name "deploy-dev ${TAG} plan")"
  _run_job create-apply-job --stack-id "${sid}" \
    --execution-plan-strategy FROM_PLAN_JOB_ID --execution-plan-job-id "${plan_jid}" \
    --display-name "deploy-dev ${TAG} apply" >/dev/null
  echo "== apply done"
}

cmd_smoke() { # gateway smoke: /api/health・SPA・ビルダーのモデル一覧(8 モデル)・chat models API
  local gw; gw="$(tf_output gateway_url)"
  [ -n "${gw}" ] && [ "${gw}" != "null" ] || { echo "gateway_url output not found"; exit 1; }
  echo "== smoke against ${gw}"
  # CI 再作成直後はコンテナ起動待ちがあるため /api/health はリトライ(最大 ~3 分)
  local i ok=""
  for i in $(seq 1 36); do
    if [ "$(curl -so /dev/null -w '%{http_code}' "${gw}/api/health")" = "200" ]; then ok=1; break; fi
    echo "  waiting for /api/health... (${i}/36)"; sleep 5
  done
  [ -n "${ok}" ] || { echo "FAIL: /api/health"; exit 1; }
  echo "  OK /api/health 200"
  local code; code="$(curl -so /dev/null -w '%{http_code}' "${gw}/")"
  [ "${code}" = "200" ] || { echo "FAIL: SPA / -> ${code}"; exit 1; }
  echo "  OK SPA / 200"
  code="$(curl -so /dev/null -w '%{http_code}' "${gw}/api/chat/models")"
  [ "${code}" = "200" ] || { echo "FAIL: /api/chat/models -> ${code}"; exit 1; }
  echo "  OK /api/chat/models 200"
  # ビルダー UI のモデル一覧はフロント同梱(state.ts GEN_MODELS)。モデル選択の実体は
  # 遅延チャンク assets/demobuilder-*.js に入る(index チャンクは i18n の接頭辞付きキーのみ)。
  # minify は引用符を "・'・` のどれにも変えうるため 3 種を許容して 8 キー全て居ることを見る
  local asset js chunk bundle m missing="" q="[\"'\`]"
  asset="$(curl -s "${gw}/" | grep -o 'assets/index[^"]*\.js' | head -1)"
  [ -n "${asset}" ] || { echo "FAIL: SPA asset not found in index.html"; exit 1; }
  js="$(curl -s "${gw}/${asset}")"
  chunk="$(echo "${js}" | grep -o 'assets/demobuilder[^"]*\.js' | head -1)"
  [ -n "${chunk}" ] || { echo "FAIL: demobuilder chunk not referenced from index chunk"; exit 1; }
  bundle="$(curl -s "${gw}/${chunk}")"
  for m in gpt-oss-120b gpt-5.5 gpt-5.6-luna gpt-5.6-sol gpt-5.6-terra \
           gpt-5.1-codex-mini gpt-5.3-codex gpt-5.5-pro; do
    echo "${bundle}" | grep -Eq "${q}${m}${q}" || missing="${missing} ${m}"
  done
  [ -z "${missing}" ] || { echo "FAIL: gen models missing in SPA bundle:${missing}"; exit 1; }
  echo "  OK gen model list (8 models) in SPA bundle"
  # 既存 ready デモの生成 SPA 配信の回帰(review-2 B001: RAG_BUCKET/APP_SESSION_SECRET 欠落で
  # /app/ 404・app-session 500 になっていた)。公開バンドル付き ready デモが無い環境では明示 SKIP
  local demo_id code
  demo_id="$(curl -s "${gw}/api/demos" | jq -r \
    '[.demos[]? | select(.status=="ready" and ((.config.frontend.bundle? // "") != ""))][0].id // empty')"
  if [ -n "${demo_id}" ]; then
    code="$(curl -so /dev/null -w '%{http_code}' -X POST "${gw}/api/demos/${demo_id}/app-session")"
    [ "${code}" = "200" ] || { echo "FAIL: app-session -> ${code} (demo ${demo_id})"; exit 1; }
    echo "  OK app-session 200 (demo ${demo_id})"
    code="$(curl -so /dev/null -w '%{http_code}' "${gw}/api/demos/${demo_id}/app/")"
    [ "${code}" = "200" ] || { echo "FAIL: demo app / -> ${code} (demo ${demo_id})"; exit 1; }
    echo "  OK ready demo app / 200 (demo ${demo_id})"
  else
    echo "  SKIP ready-demo app check (no ready demo with published bundle)"
  fi
  echo "== smoke PASS"
}

cmd_summary() { # GitHub step summary 用 Markdown。$1 = smoke step の outcome
  # (workflow が steps.smoke.outcome を渡す。success 以外を成功扱いで表示しない — review-2 M002)
  local smoke_outcome="${1:-unknown}" gw; gw="$(tf_output gateway_url)"
  cat <<EOF
## deploy-dev: ${STACK_NAME}
- branch: \`${BRANCH}\` / image tag: \`${TAG}\`
- gateway: ${gw}
- smoke(/api/health・SPA・/api/chat/models・モデル一覧8・ready デモ配信): **${smoke_outcome}**
EOF
  if [ "${smoke_outcome}" != "success" ]; then
    echo "- ⚠ smoke が success ではありません。デプロイ結果は未検証か失敗です(step ログ参照)。"
  fi
}

# SPA 同期は apply の後(SPA オブジェクトは tf 管理外 — apply がバケット/PAR を整えた後に
# 同期すれば、初回プロビジョニングやバケット再作成でも常に最終状態が正しい)
cmd_all() { cmd_image; cmd_update_stack; cmd_apply; cmd_spa; cmd_smoke; }

case "${1:-all}" in
  image)        cmd_image ;;
  spa)          cmd_spa ;;
  update-stack) cmd_update_stack ;;
  seed-env)     cmd_seed_env ;;
  apply)        cmd_apply ;;
  smoke)        cmd_smoke ;;
  summary)      cmd_summary "${2:-}" ;;
  all)          cmd_all ;;
  *) echo "usage: $0 {image|spa|update-stack|seed-env|apply|smoke|summary|all}"; exit 2 ;;
esac
