#!/usr/bin/env bash
# main→dev 同期を用意する。Public/共有の変更を main へ入れた後に実行する。
# 背景・判定の目安は CLAUDE.md「開発方式」の分岐ルール参照。
#
# 方針:
#  - 同期ブランチは refactor/* で切る（deploy-dev.yml は feat/fix/chore への push で
#    jetuse:dev へ自動配備する。refactor/* はトリガ外なので配備が走らない）。
#  - push / PR は人間ゲート。このスクリプトはやらない（案内だけ）。
#  - 衝突は自動解決しない（main の正当な変更を取りこぼさないため）。既知の構造的乖離
#    （CLAUDE.md の spikes/sp3_03_scaffold 行 / .gitignore の runs/ / docs/archive/README）は
#    dev 版=--ours が正だが、main 側が同じ箇所を本当に変えていないか確認してから解決する。
#
# 使い方: ops/sync-main-to-dev.sh [branch-name]   （既定: refactor/sync-main-dev）
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"; cd "$ROOT"
BR="${1:-refactor/sync-main-dev}"
WT="$(cd "$ROOT/.." && pwd)/_sync"

git show-ref --verify --quiet "refs/heads/$BR" && { echo "[sync] ブランチ $BR が既にある。片付けてから再実行。"; exit 1; }
[ -e "$WT" ] && { echo "[sync] worktree パス $WT が既にある。片付けてから再実行。"; exit 1; }

git fetch origin
ahead=$(git rev-list --count origin/dev..origin/main)
if [ "$ahead" -eq 0 ]; then echo "[sync] origin/main に origin/dev 未取込の commit は無い。同期不要。"; exit 0; fi
echo "[sync] origin/main=$(git rev-parse --short origin/main) → origin/dev=$(git rev-parse --short origin/dev) 同期（+$ahead commit）"
echo "[sync] 取り込む main 側 commit:"; git log --oneline origin/dev..origin/main | sed 's/^/    /'

git worktree add "$WT" -b "$BR" origin/dev
set +e; git -C "$WT" merge --no-ff --no-edit origin/main; rc=$?; set -e

if [ "$rc" -ne 0 ]; then
  echo "[sync] 衝突あり:"; git -C "$WT" diff --name-only --diff-filter=U | sed 's/^/    /'
  echo "[sync] 解決（各ファイルを確認して）: git -C \"$WT\" checkout --ours <file> && git -C \"$WT\" add <file>"
  echo "[sync]   既知の構造的乖離は dev 版(--ours)が正: CLAUDE.md(sp3_03_scaffold 行)/.gitignore(runs/)/docs/archive/README"
  echo "[sync]   ※ ただし main が同じ箇所を本当に変えていないか diff を確認してから --ours すること"
  echo "[sync] 解決後: git -C \"$WT\" commit"
else
  echo "[sync] 衝突なしでマージ済。"
fi
echo "[sync] 次（人間ゲート）: git push origin \"$BR\" && gh pr create --repo <owner/repo> --base dev --head \"$BR\""
echo "[sync] 後始末（PR作成後）: git worktree remove \"$WT\""
