#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
output_dir="${1:-${repo_root}/dist/orm}"

mkdir -p "${output_dir}"
output_dir="$(cd "${output_dir}" && pwd)"

if [[ ! -f "${repo_root}/packages/web/dist/index.html" ]]; then
  echo "packages/web/dist/index.html is missing; build the SPA before packaging" >&2
  exit 1
fi

work_dir="$(mktemp -d "${TMPDIR:-/tmp}/jetuse-orm-packages.XXXXXX")"
trap 'rm -rf "${work_dir}"' EXIT

source_tree="${work_dir}/source"
app_stage="${work_dir}/jetuse-orm"
mkdir -p "${source_tree}" "${app_stage}"

# Copy only tracked Terraform files. This prevents local .terraform directories
# and other ignored build artifacts from leaking into the public archives.
#
# 既定は HEAD（配布物は必ずコミット済みの内容から作る）。
# `PACKAGE_FROM_WORKTREE=1` のときだけ**作業ツリー**から作る。
# これは手元の検査（ops/check-infra.sh）専用で、未コミットの変更でも
# 「梱包すると壊れる」を検出できるようにするため。**配布には使わない。**
if [[ "${PACKAGE_FROM_WORKTREE:-0}" == "1" ]]; then
  echo "[package] 作業ツリーから梱包します（検査用。配布には使わないこと）" >&2
  # 追跡ファイル + 未追跡(ignore されていない)ファイル。無視対象は入れない
  # `--cached` は**作業ツリーで削除済みの追跡ファイル**も並べるので、
  # 削除・rename があると tar が存在しないパスを読んで失敗する。実在するものだけに絞る。
  git -C "${repo_root}" ls-files --cached --others --exclude-standard --deduplicate \
    -- infra/orm infra/terraform/modules \
    | ( cd "${repo_root}" && while IFS= read -r f; do [ -f "$f" ] && printf '%s\n' "$f"; done ) \
    | tar -cf - -C "${repo_root}" -T - \
    | tar -xf - -C "${source_tree}"
else
  git -C "${repo_root}" archive --format=tar HEAD \
    infra/orm \
    infra/terraform/modules \
    | tar -xf - -C "${source_tree}"
fi

# Resource Manager runs from the root of a Deploy to Oracle Cloud archive.
# Relocate each entry point and rewrite its repository-relative module paths.
cp -R "${source_tree}/infra/orm/." "${app_stage}/"
mkdir -p "${app_stage}/terraform" "${app_stage}/packages/web"
cp -R "${source_tree}/infra/terraform/modules" "${app_stage}/terraform/"
cp -R "${repo_root}/packages/web/dist" "${app_stage}/packages/web/"
# in-place 置換は使わない: GNU と BSD(macOS) で `sed -i` の引数解釈が異なり、macOS では
# 置換式がバックアップ拡張子として食われて壊れる。開発者がローカルでも zip を検証できるよう、
# 一時ファイル経由の移植可能な形にする。
rewrite() { # rewrite <file> <sed-expr>
  sed "$2" "$1" > "$1.tmp" && mv "$1.tmp" "$1"
}
rewrite "${app_stage}/main.tf" 's#../terraform/modules/#./terraform/modules/#g'
rewrite "${app_stage}/spa.tf" 's#${path.module}/../../packages/web/dist#${path.module}/packages/web/dist#g'

# 配布ZIPの画像タグは、そのビルドの commit SHA に固定する（PORT-03）。
# `latest` のままだと、新しい release が同じタグへ push しても Terraform には差分が出ず、
# コンテナの修正がスタック更新で反映されない。さらに API とエージェントは invoke ステートの
# 契約を共有するため、片方だけ動くと新旧が混在しうる。1つの image_tag で束ねて同時に上げる。
# release.yml は latest と ${GITHUB_SHA} の両方を push しているので、SHA タグは必ず存在する。
# ローカル検証（GITHUB_SHA 未設定）では latest のままにする。
if [[ -n "${GITHUB_SHA:-}" ]]; then
  rewrite "${app_stage}/variables.tf" \
    "/^variable \"image_tag\"/,/^}/ s|^  default     = \"latest\"$|  default     = \"${GITHUB_SHA}\"|"
  rewrite "${app_stage}/schema.yaml" \
    "/^  image_tag:/,/^$/ s|^    default: \"latest\"$|    default: \"${GITHUB_SHA}\"|"
  if ! grep -q "default     = \"${GITHUB_SHA}\"" "${app_stage}/variables.tf" \
    || ! grep -q "default: \"${GITHUB_SHA}\"" "${app_stage}/schema.yaml"; then
    echo "failed to pin image_tag to ${GITHUB_SHA}" >&2
    exit 1
  fi
fi

if find "${app_stage}" -type d -name .terraform -print -quit | grep -q .; then
  echo "unexpected .terraform directory in ${app_stage}" >&2
  exit 1
fi
if grep -R -n -E \
  'source[[:space:]]*=[[:space:]]*"\.\./terraform/modules|spa_dist_dir[[:space:]]*=.*\.\./\.\./packages/web/dist' \
  "${app_stage}"; then
  echo "repository-relative path remains in ${app_stage}" >&2
  exit 1
fi

(cd "${app_stage}" && zip -q -r "${work_dir}/jetuse-orm.zip" .)

install -m 0644 "${work_dir}/jetuse-orm.zip" "${output_dir}/jetuse-orm.zip"

echo "Created ${output_dir}/jetuse-orm.zip"
