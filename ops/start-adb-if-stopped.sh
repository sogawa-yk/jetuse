#!/usr/bin/env bash
# 夜間停止後に共有 ADB が STOPPED のままになる問題(backlog #10)の対策。
# dev計算インスタンスのopcユーザーcronから毎朝実行する想定(導入は人間判断)。
# 例: crontab -e で「30 8 * * 1-5 /home/opc/jetuse/ops/start-adb-if-stopped.sh >> /tmp/adb-start.log 2>&1」
#
# 使い方: ops/start-adb-if-stopped.sh [env]
#   env は internal-dev（既定）| public-dev | all。ops/orm-stack.sh と同じ対応表で解決する。
#   ADB 名を直接指定したいときは JETUSE_ADB_NAME を渡す。
set -euo pipefail

# `date -Is` は GNU 専用。macOS(BSD date) では "invalid argument" になる
# (このスクリプトは OL9 インスタンスの cron 前提で書かれていた)。両方で動く形にする。
ts() { date "+%Y-%m-%dT%H:%M:%S%z"; }

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=ops/_region.sh
. "$ROOT/ops/_region.sh"
[ -f "$ROOT/.env" ] && set -a && . "$ROOT/.env" && set +a

# **環境ごとにコンパートメントが違う。** 以前は常に `COMPARTMENT_OCID`(=internal-dev)を
# 見ていたため、`orm-stack.sh public-dev` が「ADB が STOPPED」と案内してこのスクリプトを
# 呼んでも、**public-dev の ADB は永久に見つからなかった**(2026-08-08 のレビュー指摘)。
# 対応表は orm-stack.sh と揃える。
env_compartment() {
  case "$1" in
    internal-dev) echo "${INTERNAL_DEV_COMPARTMENT_OCID:-${COMPARTMENT_OCID:-}}" ;;
    public-dev)   echo "${PUBLIC_DEV_COMPARTMENT_OCID:-}" ;;
    *) return 1 ;;
  esac
}
env_adb_name() {
  case "$1" in
    internal-dev) echo "jetuse-dev-adb" ;;      # prefix=jetuse-dev + "-adb"
    public-dev)   echo "jetuse-pubdev-adb" ;;   # prefix=jetuse-pubdev + "-adb"
    *) return 1 ;;
  esac
}

TARGET="${1:-internal-dev}"
case "$TARGET" in
  all) ENVS="internal-dev public-dev" ;;
  internal-dev|public-dev) ENVS="$TARGET" ;;
  *) echo "未知の env: $TARGET （internal-dev | public-dev | all）" >&2; exit 2 ;;
esac

# **リージョンを決め打ちにしない。** 以前は `--region` を渡さず既定リージョン(大阪)だけを
# 見ていた。シカゴ移行(ADR-0027)で jetuse-dev-adb が us-chicago-1 へ移った後、この
# スクリプトは**対象を見つけられないまま「nothing to do」と言って正常終了**していた
# (2026-08-08 実測。ADB が止まったまま ORM の apply が 409 IncorrectState で落ちて発覚)。
# 候補は `_region.sh` の対応表が正(直書きしない)。
REGIONS="${ADB_REGIONS:-$(jetuse_known_regions)}"

# **握り潰さない。** 以前は各リージョンの CLI エラーを `|| true` で捨て、どこか1つで
# 見つかれば成功扱いにしていた。一部リージョンが認証・通信で落ちていても
# 「確認した」ことになり、止まったままの ADB を見逃す(2026-08-09 のレビュー指摘)。
# env ごとに「探索できたか」「見つかったか」を別々に数え、どちらも満たさなければ失敗させる。
# **stderr を値に混ぜない。** `state=$(... 2>&1)` にすると、CLI が該当0件のときに出す
# 「Query returned empty result」という**案内文が値として入り**、「見つかった」と誤って数える
# (2026-08-09 実測: 大阪に無い ADB を「複数リージョンにある」と誤判定した)。
ERRF="$(mktemp)"
trap 'rm -f "$ERRF"' EXIT

rc=0
for e in $ENVS; do
  comp="$(env_compartment "$e")"
  if [ -z "$comp" ]; then
    echo "$(ts) ${e}: コンパートメント OCID が .env に無い" >&2
    rc=1
    continue
  fi
  name="${JETUSE_ADB_NAME:-$(env_adb_name "$e")}"
  # **調べ切ってから動かす。** 逐次に起動すると、最初のリージョンで見つけた個体を
  # 起動した**後で**別リージョンの重複に気づくことになる。「重複なら止める」と言いながら
  # 既に変更済み、では fail-closed になっていない(2026-08-09 のレビュー指摘)。
  found=0
  hits=""
  scan_failed=0
  for r in $REGIONS; do
    # **`[0]` で単一値に潰さない。** 同じコンパートメント・同じリージョンに同名 ADB が
    # 複数あると、先頭だけ見て残りの STOPPED を見逃す（あるいは意図しない個体を起動する）。
    # 配列で受けて件数を数える。
    if ! rows=$(oci db autonomous-database list -c "$comp" --region "$r" \
          --query "data[?\"display-name\"=='${name}'].{id:id,state:\"lifecycle-state\"}" \
          --output json 2>"$ERRF"); then
      echo "$(ts) ${e}/${r}: 検索に失敗（認証・通信を確認）: $(tr -d '\n' < "$ERRF" | cut -c1-200)" >&2
      rc=1
      scan_failed=1
      continue
    fi
    # **空一致のとき CLI は `[]` ではなく「何も出さない」。** 素の json.load は落ちるので、
    # 空文字は 0 件として扱う（「解釈できない」と混同すると全リージョンで誤警告になる）。
    n=$(printf '%s' "$rows" | python3 -c '
import json, sys
raw = sys.stdin.read().strip()
if not raw:
    print(0); raise SystemExit
try:
    print(len(json.loads(raw) or []))
except Exception:
    print(-1)
')
    if [ "$n" -lt 0 ]; then
      echo "$(ts) ${e}/${r}: 応答を解釈できない" >&2; rc=1; scan_failed=1; continue
    fi
    [ "$n" -eq 0 ] && continue
    if [ "$n" -gt 1 ]; then
      echo "$(ts) ${name} が ${e}/${r} に ${n} 台ある。想定外なので止める。" >&2
      rc=1
      scan_failed=1
      continue
    fi
    # **ここでは起動しない。** 候補として控えるだけ。
    found=$((found + 1))
    hits="${hits}${r} $(printf '%s' "$rows" \
      | python3 -c 'import json,sys; d=json.load(sys.stdin)[0]; print(d["id"], d["state"])')
"
  done

  # --- 全リージョンを調べ切ってから判断する ---
  # 逐次に起動すると、最初のリージョンで見つけた個体を起動した**後で**重複や検索失敗に
  # 気づくことになる。「重複なら止める」と言いながら既に変更済みでは fail-closed でない。
  if [ "$scan_failed" -ne 0 ]; then
    echo "$(ts) ${e}: 一部リージョンを確認できなかった。何も起動せずに見送る。" >&2
    continue
  fi
  if [ "$found" -gt 1 ]; then
    echo "$(ts) ${name} が複数リージョンにある（${e}）。何も起動せずに止める。" >&2
    rc=1
    continue
  fi
  # **「止まっていない」と「そもそも見つからない」を混同しない。**
  if [ "$found" -eq 0 ]; then
    echo "$(ts) ${name} が見つからない (env: ${e} / 探索: ${REGIONS})" >&2
    echo "  コンパートメントかリージョン、ADB 名の想定が変わっている。" >&2
    echo "  ADB_REGIONS / JETUSE_ADB_NAME で明示できる。" >&2
    rc=1
    continue
  fi

  # ここまで来たら候補はちょうど1つ。
  set -- $hits
  r="$1"; id="$2"; state="$3"
  # **AVAILABLE 以外を「問題なし」に丸めない。** `orm-stack.sh` は AVAILABLE でない限り
  # このヘルパーを案内するので、STARTING / STOPPING / UNAVAILABLE を "nothing to do" で
  # 成功終了すると、呼び出し側が「起動した」と思って apply へ進み 409 で落ちる
  # (2026-08-09 のレビュー指摘)。
  case "$state" in
    AVAILABLE)
      echo "$(ts) ${name} (${e}/${r}) is AVAILABLE — nothing to do"
      continue
      ;;
    STOPPED) ;;   # ここだけが起動してよい状態
    STARTING|STOPPING|SCALE_IN_PROGRESS|UPDATING|MAINTENANCE_IN_PROGRESS)
      echo "$(ts) ${name} (${e}/${r}) は ${state}。遷移中なので触らない。落ち着いてから再実行を。" >&2
      rc=1
      continue
      ;;
    *)
      echo "$(ts) ${name} (${e}/${r}) が ${state}。起動できる状態ではない。" >&2
      rc=1
      continue
      ;;
  esac
  echo "$(ts) starting ${name} (${e}/${r})"
  oci db autonomous-database start --autonomous-database-id "$id" --region "$r" \
    --wait-for-state AVAILABLE >/dev/null
  echo "$(ts) ${name} (${e}/${r}) is AVAILABLE"
done
exit "$rc"
