#!/usr/bin/env bash
# infra（Terraform）の静的検査＋資格情報不要のモジュールテスト。
# **CI の terraform ジョブと同じ検査項目**を手元で回すための入口。
# 梱包検査だけは**作業ツリー**から作る（CI はコミット後に走るので HEAD で足りるが、
# 手元では未コミットの変更を見ないと「梱包すると壊れる」を見逃す）。
#
# なぜ要るか（AGT-06 の実害）:
#   CI は infra を検査するのに、`make lint` もループの完了ゲートも
#   **web / api しか見ていなかった**。infra を触ると「ループは緑・CI は赤」になる。
#   実際 AGT-06 で `variables.tf` 2 本が CI で落ちた。
#   `terraform validate` は構文と整合性を見るが**書式は見ない**ので、fmt と両方要る。
#
# 使い方: ops/check-infra.sh          # 検査のみ（CI と同じ。書き換えない）
#         ops/check-infra.sh --fix    # 書式を直す
set -euo pipefail
cd "$(dirname "$0")/.."

FIX=0
for a in "$@"; do case "$a" in --fix) FIX=1;; *) echo "unknown flag: $a" >&2; exit 2;; esac; done

# infra に変更があるか。**未コミットの差分だけを見ない** —— ブランチで既にコミット済みだと
# 空になり、infra を変えたのに未検査で緑になってしまう(review-2)。
# 基準ブランチとの差分も見る(基準が取れない環境では「変更あり」に倒す = 安全側)。
# 基準ブランチは 1 つに決められない —— このリポジトリは public-dev 起点と internal-dev 起点の
# 両方がある (docs/guides/branching-and-releases.md)。**どれかとの差分が空なら「変更なし」**
# とみなす。1 つだけを見ると、別系統由来のブランチで常に「変更あり」になる(review-3)。
# main / internal-stable も含めるのは、release ブランチから切った hotfix を拾うため。
INFRA_BASES="${INFRA_BASE_REF:-origin/public-dev origin/internal-dev origin/main origin/internal-stable}"
# 検査対象は infra だけではない —— **検査の仕組み自体**を壊す変更こそ検知したい。
# これらを外すと、terraform を入れていない環境で検査機構を壊しても素通りする(review-5)。
WATCH="infra ops/check-infra.sh scripts/package-orm-stacks.sh Makefile loop-config.yml .claude"
infra_changed() {
  # shellcheck disable=SC2086
  [ -n "$(git status --porcelain -- $WATCH 2>/dev/null)" ] && return 0
  _seen=0
  for b in $INFRA_BASES; do
    git rev-parse --verify --quiet "$b" >/dev/null 2>&1 || continue
    _seen=1
    # **終了ステータスを見る。** 浅い clone で merge-base が無いと git は失敗して空を返すので、
    # 空文字だけで「変更なし」と判断すると未検査のまま緑になる(review-6)
    # shellcheck disable=SC2086
    if _d=$(git diff --name-only "$b"...HEAD -- $WATCH 2>/dev/null) && [ -z "$_d" ]; then
      return 1
    fi
  done
  # 基準がどれも無い(浅い clone 等)なら判断できないので検査する側へ倒す
  [ "$_seen" = 0 ] && return 0
  return 0
}

# terraform が無い環境の扱い:
#   **infra に変更があるなら止める。** 無検査のまま「クリーン」と判定すると、
#   この入口を足した意味（ループが緑なら CI も緑）が失われる。
#   infra を触っていないなら、web / api だけの開発者を止める理由が無いのでスキップする。
if ! command -v terraform >/dev/null 2>&1; then
  if infra_changed; then
    echo "[infra] terraform が無いのに infra に変更があります。" >&2
    echo "[infra] 未検査のまま緑にはしません（CI は必ず検査します）。terraform を入れてください。" >&2
    exit 1
  fi
  echo "[infra] terraform が無く infra の変更も無いのでスキップ" >&2
  exit 0
fi

if [ "$FIX" = 1 ]; then
  echo "[infra] terraform fmt -recursive infra"
  terraform fmt -recursive infra
else
  echo "[infra] terraform fmt -check -recursive infra"
  # 落ちたら「どう直すか」を出す。CI のログだけだと直し方が分からない
  terraform fmt -check -recursive infra || {
    echo "[infra] 書式が崩れています。'ops/check-infra.sh --fix' で直せます。" >&2
    exit 1
  }
fi

# CI と同じ対象を validate する（backend 無しの init なので資格情報を要求しない）
for d in infra/terraform/environments/dev infra/orm; do
  echo "[infra] terraform validate: $d"
  ( cd "$d" && terraform init -backend=false -input=false -lockfile=readonly >/dev/null && terraform validate >/dev/null )
done

# CI と同じモジュールテスト（mock provider / mock CLI。資格情報も課金も発生しない）。
# `mock_provider` は **Terraform 1.7 以降**。CI は setup-terraform@v3 が最新を入れるが、
# 手元が古いと実行できない。**infra に変更があるなら止める**（未検査で緑にしない）。
TF_VER=$(terraform version -json 2>/dev/null | sed -n 's/.*"terraform_version": *"\([^"]*\)".*/\1/p')
[ -n "$TF_VER" ] || TF_VER=$(terraform version | head -1 | sed 's/[^0-9.]*//')
TF_MAJOR=${TF_VER%%.*}; TF_REST=${TF_VER#*.}; TF_MINOR=${TF_REST%%.*}
if [ "${TF_MAJOR:-0}" -gt 1 ] || { [ "${TF_MAJOR:-0}" -eq 1 ] && [ "${TF_MINOR:-0}" -ge 7 ]; }; then
  for d in infra/terraform/modules/iam infra/terraform/modules/hosted-agent; do
    echo "[infra] terraform test: $d"
    ( cd "$d" && terraform init -backend=false -input=false -lockfile=readonly >/dev/null && terraform test )
  done
elif infra_changed; then
  echo "[infra] terraform $TF_VER では terraform test（mock_provider）が使えません。" >&2
  echo "[infra] infra に変更があるので未検査で緑にはしません。1.7 以降へ更新してください。" >&2
  exit 1
else
  echo "[infra] terraform $TF_VER は terraform test 非対応。infra の変更が無いのでスキップ" >&2
fi

# terraform test は local-exec を実行しないので、シェル側の判断はモックで確認する（CI と同じ）
echo "[infra] hosted agent CLI smoke test"
sh infra/terraform/modules/hosted-agent/scripts/smoke_test.sh

# CI の梱包検査（ワンクリックスタックの zip を作り、必須ファイル・schema key・
# パス書換え後の validate まで見る）。ここまでやって初めて「CI と同じ」と言える
TMPD=$(mktemp -d)
trap 'rm -rf "$TMPD"' EXIT
# 梱包検査は**作業ツリー**から作る。既定の `git archive HEAD` だと未コミットの変更が
# 反映されず、「梱包すると壊れる」変更を古い HEAD で緑にしてしまう(review-4)。
# 配布物は従来どおり HEAD から作る(この環境変数は検査専用)。
echo "[infra] package + validate ORM stacks (作業ツリー基準)"
# 失敗の原因が読めるようにする。成功時だけ静かにする（stdout は捨て、失敗したら stderr を出す）
if ! PACKAGE_FROM_WORKTREE=1 bash scripts/package-orm-stacks.sh "$TMPD/orm-packages" \
     >"$TMPD/package.out" 2>&1; then
  echo "[infra] 梱包に失敗しました:" >&2
  cat "$TMPD/package.out" >&2
  exit 1
fi
mkdir -p "$TMPD/orm-app"
unzip -q "$TMPD/orm-packages/jetuse-orm.zip" -d "$TMPD/orm-app"
for f in schema.yaml main.tf; do
  [ -f "$TMPD/orm-app/$f" ] || { echo "[infra] 梱包に $f が無い" >&2; exit 1; }
done
for k in enable_dynamic_group enable_runtime_policy; do
  grep -q "^  $k:" "$TMPD/orm-app/schema.yaml" || {
    echo "[infra] schema.yaml に $k が無い" >&2; exit 1; }
done
terraform -chdir="$TMPD/orm-app" init -backend=false -input=false >/dev/null
terraform -chdir="$TMPD/orm-app" validate >/dev/null

echo "[infra] OK"
