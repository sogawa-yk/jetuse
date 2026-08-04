#!/usr/bin/env bash
# public-dev→internal-dev 同期を用意する。Public/共有の変更を public-dev へ入れた後に実行する。
# 背景・判定の目安は CLAUDE.md「開発方式」の分岐ルールと docs/guides/branching-and-releases.md 参照。
#
# 前提（4ブランチ体制 / ADR-0028）:
#  - Internal ⊇ Public。同期は public-dev → internal-dev の一方向のみ。
#    逆向き（internal-dev → public-dev）は merge しない。merge はブランチ先端を丸ごと運ぶため
#    内部固有機能が Public に漏れる。後から公開する変更は最新 public-dev 上へ cherry-pick する。
#  - main / internal-stable は release 先であり、同期には関与しない。
#
# 方針:
#  - 同期ブランチは refactor/* で切る（deploy-dev.yml は feat/fix/chore への push で
#    jetuse:dev へ自動配備する。refactor/* はトリガ外なので配備が走らない）。
#  - push / PR は人間ゲート。このスクリプトはやらない（案内だけ）。
#  - 衝突は自動解決しない（public-dev の正当な変更を取りこぼさないため）。
#
# 使い方: ops/sync-public-to-internal.sh [branch-name]   （既定: refactor/sync-public-internal）
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"; cd "$ROOT"
BR="${1:-refactor/sync-public-internal}"
WT="$(cd "$ROOT/.." && pwd)/_sync"
SRC="origin/public-dev"
DST="origin/internal-dev"

git show-ref --verify --quiet "refs/heads/$BR" && { echo "[sync] ブランチ $BR が既にある。片付けてから再実行。"; exit 1; }
[ -e "$WT" ] && { echo "[sync] worktree パス $WT が既にある。片付けてから再実行。"; exit 1; }

git fetch origin
for r in "$SRC" "$DST"; do
  git rev-parse --verify --quiet "$r" >/dev/null \
    || { echo "[sync] $r が無い。git fetch するか、4ブランチ体制への移行が済んでいるか確認。"; exit 1; }
done

ahead=$(git rev-list --count "$DST".."$SRC")
if [ "$ahead" -eq 0 ]; then echo "[sync] $SRC に $DST 未取込の commit は無い。同期不要。"; exit 0; fi
echo "[sync] $SRC=$(git rev-parse --short "$SRC") → $DST=$(git rev-parse --short "$DST") 同期（+$ahead commit）"
echo "[sync] 取り込む public-dev 側 commit:"; git log --oneline "$DST".."$SRC" | sed 's/^/    /'

git worktree add "$WT" -b "$BR" "$DST"
set +e; git -C "$WT" merge --no-ff --no-edit "$SRC"; rc=$?; set -e

if [ "$rc" -ne 0 ]; then
  echo "[sync] 衝突あり:"; git -C "$WT" diff --name-only --diff-filter=U | sed 's/^/    /'
  echo "[sync] 解決（各ファイルを確認して）: git -C \"$WT\" checkout --ours <file> && git -C \"$WT\" add <file>"
  echo "[sync]   内部固有ファイル（ops/internal-only-paths.txt に列挙）は internal 版(--ours)が正。"
  echo "[sync]   それ以外は public-dev 側の変更を取りこぼしていないか diff を確認してから解決する。"
  echo "[sync] 解決後: git -C \"$WT\" commit"
else
  echo "[sync] 衝突なしでマージ済。"
fi
echo "[sync] 次（人間ゲート）: git push origin \"$BR\" && gh pr create --repo <owner/repo> --base internal-dev --head \"$BR\""
echo "[sync] 後始末（PR作成後）: git worktree remove \"$WT\""
