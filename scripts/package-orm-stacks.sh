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
git -C "${repo_root}" archive --format=tar HEAD \
  infra/orm \
  infra/terraform/modules \
  | tar -xf - -C "${source_tree}"

# Resource Manager runs from the root of a Deploy to Oracle Cloud archive.
# Relocate each entry point and rewrite its repository-relative module paths.
cp -R "${source_tree}/infra/orm/." "${app_stage}/"
mkdir -p "${app_stage}/terraform" "${app_stage}/packages/web"
cp -R "${source_tree}/infra/terraform/modules" "${app_stage}/terraform/"
cp -R "${repo_root}/packages/web/dist" "${app_stage}/packages/web/"
sed -i 's#../terraform/modules/#./terraform/modules/#g' "${app_stage}/main.tf"
sed -i 's#${path.module}/../../packages/web/dist#${path.module}/packages/web/dist#g' \
  "${app_stage}/spa.tf"

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
