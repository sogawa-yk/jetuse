#!/usr/bin/env bash
# worktree にタスク実行用の隔離環境を用意する（冪等）。start-loop.sh から呼ばれる。
#
# なぜ worktree ごとに環境を作るか:
#   packages/api は editable install。共有 .venv をそのまま使うと import が「元リポジトリの
#   ソース」を指し、worktree 内の編集がテストに反映されない（隔離が破れる）。よって worktree
#   専用の .venv を作り、worktree のソースを editable install する。pip/npm のキャッシュは
#   ユーザ共通なので2回目以降は高速。
#
# 対象 area は tasks/<task>.md の「対象 area」行から推定（api / web）。読めなければ api を既定。
# LOOP_SKIP_BOOTSTRAP=1 で start-loop.sh 側からスキップ可能。
set -euo pipefail

WT="${1:?usage: bootstrap-env.sh <worktree-dir> [task-id]}"
TASK="${2:-}"
cd "$WT"

AREAS=""
if [ -n "$TASK" ] && [ -f "tasks/${TASK}.md" ]; then
  AREAS="$(grep -iE '対象 *area' "tasks/${TASK}.md" 2>/dev/null | grep -oiE 'web|api' | tr 'A-Z' 'a-z' | sort -u | tr '\n' ' ' || true)"
fi
[ -z "${AREAS// /}" ] && AREAS="api"   # 既定: api（ステージ1の主戦場）

want() { case " $AREAS " in *" $1 "*) return 0;; *) return 1;; esac; }

# --- api: 専用 .venv + editable install ---
if want api && [ -f packages/api/pyproject.toml ]; then
  if [ ! -x .venv/bin/python ]; then
    # ローカル macOS=3.13 / dev インスタンス(OL9.7)=3.12 の両対応（2026-07-28）
    PY_BIN="$(command -v python3.13 || command -v python3.12 || command -v python3)" \
      || { echo "[bootstrap] api: python3.13 / python3.12 / python3 が見つからない" >&2; exit 1; }
    echo "[bootstrap] api: .venv 作成（${PY_BIN##*/}）" >&2
    "$PY_BIN" -m venv .venv
    .venv/bin/python -m pip install -q --upgrade pip
  fi
  echo "[bootstrap] api: packages/api を editable install" >&2
  ( cd packages/api && "$WT/.venv/bin/python" -m pip install -q -e ".[dev]" )
fi

# --- web: node_modules ---
if want web && [ -f packages/web/package.json ] && [ ! -d packages/web/node_modules ]; then
  echo "[bootstrap] web: 依存インストール（npm ci）" >&2
  ( cd packages/web && { npm ci --silent || npm install --silent; } )
fi

# --- infra: terraform のプロバイダ ---
# **`.terraform/` は gitignore なので新しい worktree には無い。** 無いまま `make lint` を打つと
# `ops/check-infra.sh` の `terraform init -lockfile=readonly` が
# "Provider dependency changes detected" で落ちる（2026-08-20 の vid ステージで実際に踏んだ）。
# backend 無しの init なので資格情報も課金も発生しない。**失敗しても続行する**
# （terraform 未導入の環境でブートストラップ全体を止めない）。
if want infra && command -v terraform >/dev/null 2>&1; then
  for d in infra/terraform/environments/dev infra/orm \
           infra/terraform/modules/iam infra/terraform/modules/hosted-agent; do
    [ -d "$d" ] || continue
    ( cd "$d" && terraform init -backend=false -input=false >/dev/null 2>&1 ) \
      || echo "[bootstrap] infra: $d の init に失敗（make lint 前に手で init が要ります）" >&2
  done
  echo "[bootstrap] infra: terraform init（backend 無し）" >&2
fi

echo "[bootstrap] 完了: $WT (areas: ${AREAS})" >&2
