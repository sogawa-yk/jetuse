#!/usr/bin/env bash
# 前提チェック。**使う前に「何が足りないか」を1回で出す。**
#
# なぜ要るか: これまで前提の欠落は**実行の途中で**露見していた。とくに `codex` は
# レビューゲートそのもので、無いとループは実装まで進んでからレビューで落ちる
# （onboarding にも記載が無かった）。「確認できないものを問題なしとして進む」を止める。
#
# 使い方: make doctor  /  ops/doctor.sh
#   HARD が1つでも欠けると非ゼロで終わる。SOFT は警告だけ（作業は続けられる）。
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

HARD_MISSING=0
SOFT_MISSING=0

# 色は付けない（ログに残す・CI で読む用途があるため）。
row() { printf '  %-9s %-14s %-22s %s\n' "$1" "$2" "$3" "$4"; }

hard() {  # name, found(0/1), detail, fix
  if [ "$2" -eq 0 ]; then row "OK" "$1" "$3" ""
  else row "NG" "$1" "$3" "$4"; HARD_MISSING=$((HARD_MISSING + 1)); fi
}
soft() {
  if [ "$2" -eq 0 ]; then row "OK" "$1" "$3" ""
  else row "warn" "$1" "$3" "$4"; SOFT_MISSING=$((SOFT_MISSING + 1)); fi
}

has() { command -v "$1" >/dev/null 2>&1; }
ver() { "$@" 2>/dev/null | head -1; }

echo "== 必須（欠けると作業が進まない）"

# --- レビューゲート -----------------------------------------------------------
# **これが本命。** 完了条件の1つ（review_verdict=PASS）は codex なしでは満たせない。
if has codex; then
  # **版が出ても認証済みとは限らない。** 未ログイン・期限切れはレビュー実行時まで分からない。
  # 非対話で安全に確かめる手が無いので、ここでは「導入」までを保証し、そう明示する。
  hard codex 0 "$(ver codex --version | cut -c1-16)（認証は未検証）" ""
else
  hard codex 1 "なし" "レビューが回りません。npm i -g @openai/codex 等で導入し認証する"
fi

# --- OCI ----------------------------------------------------------------------
if has oci; then
  hard "oci" 0 "$(ver oci --version | cut -c1-12)" ""
else
  hard "oci" 1 "なし" "実環境 E2E ができません。OCI CLI を導入する"
fi
# **AUTH_MODE で要否が変わる。** config_file(既定) のときだけ ~/.oci/config が要る。
# インスタンス実行(instance_principal)や配備先(resource_principal)では不要で、
# そこで必須にすると**正しい環境が落ちる**。
AM="${AUTH_MODE:-}"
# **空白を全部消さない。** `AUTH_MODE=config_file # local` が `config_file#local` になり
# 未知の値として弾かれる。行末コメントを落としてから前後の空白だけ取る。
[ -z "$AM" ] && [ -f .env ] && AM="$(awk -F= '/^AUTH_MODE=/{sub(/#.*/,"",$2); gsub(/^[ \t\r]+|[ \t\r]+$/,"",$2); print $2; exit}' .env)"
AM="${AM:-config_file}"
# **未知の値を「不要」に倒さない。** typo や dotenv の引用付き（AUTH_MODE="config_file"）を
# 黙って通すと、アプリ側が解決できない設定のまま前提チェックだけ緑になる。
AM="$(printf '%s' "$AM" | tr -d '"'"'"'')"
case "$AM" in
  config_file)
    if [ -f "$HOME/.oci/config" ]; then
      # **プロファイル名まで確かめる。** セクション数だけ見ても、`.env` の OCI_PROFILE が
      # 実在しなければ認証は実行時に落ちる（既定は DEFAULT）。
      PROF="${OCI_PROFILE:-}"
      [ -z "$PROF" ] && [ -f .env ] \
        && PROF="$(awk -F= '/^OCI_PROFILE=/{print $2;exit}' .env | tr -d ' \r"'"'"'')"
      PROF="${PROF:-DEFAULT}"
      if grep -q "^\[${PROF}\]" "$HOME/.oci/config" 2>/dev/null; then
        # **セクション名だけでは足りない。** 空の [DEFAULT] や鍵の欠けたプロファイルでも
        # 通ってしまい、認証は実行時に落ちる。API キー認証に要る4つを見る。
        # **この python の失敗を握り潰さない。** 出力が空＝問題なし、と読むと
        # 例外で落ちたときに「OK」になってしまう。終了状態を別に見る。
        if MISS="$(PROF="$PROF" python3 - "$HOME/.oci/config" <<'PYOCI'
import configparser, os, sys
# `%` を含む値（パスやパスフレーズ）で補間エラーにならないよう RawConfigParser を使う。
cp = configparser.RawConfigParser()
try:
    if not cp.read(sys.argv[1]):
        print("読めない"); sys.exit(0)
except Exception as e:
    print("解釈できない: %s" % type(e).__name__); sys.exit(0)
p = os.environ["PROF"]
if not cp.has_section(p) and p != "DEFAULT":
    print("セクションが無い"); sys.exit(0)
sec = dict(cp.items(p)) if cp.has_section(p) else dict(cp.defaults())
# セッショントークン認証でも**署名鍵は要る**。不要になるのは user / fingerprint だけ。
token = sec.get("security_token_file")
need = ("tenancy", "key_file") if token else ("tenancy", "user", "fingerprint", "key_file")
missing = [k for k in need if not sec.get(k)]
# 指しているだけで実体が無い／読めないファイルは、実行時に初めて落ちる。
for key in ("key_file", "security_token_file"):
    v = sec.get(key)
    if v and key not in missing:
        path = os.path.expanduser(v)
        if not os.path.isfile(path):
            missing.append("%s が指す先が無い" % key)
        elif not os.access(path, os.R_OK):
            missing.append("%s が読めない" % key)
print(" ".join(missing))
PYOCI
)"; then :; else
          hard "oci config" 1 "検査できない" "python3 で ~/.oci/config を読めない"
          MISS="__ERR__"
        fi
        if [ "$MISS" = "__ERR__" ]; then :
        elif [ -z "$MISS" ]; then hard "oci config" 0 "プロファイル ${PROF}" ""
        else hard "oci config" 1 "${PROF}: ${MISS}" "~/.oci/config を見直す"; fi
      else
        hard "oci config" 1 "${PROF} が無い" "~/.oci/config に [${PROF}] を作るか OCI_PROFILE を直す"
      fi
    else
      hard "oci config" 1 "なし" "AUTH_MODE=config_file なので ~/.oci/config が要る"
    fi
    ;;
  instance_principal|resource_principal)
    row "OK" "oci config" "不要" "AUTH_MODE=${AM}"
    ;;
  *)
    hard "oci config" 1 "AUTH_MODE=${AM}" "未知の値。config_file / instance_principal / resource_principal のいずれか"
    ;;
esac

# --- Terraform ----------------------------------------------------------------
# **1.7 未満だと terraform test の mock_provider が動かず make lint が落ちる**（CLAUDE.md）。
if has terraform; then
  TFV="$(terraform version -json 2>/dev/null \
        | python3 -c 'import json,sys;print(json.load(sys.stdin)["terraform_version"])' 2>/dev/null \
        || ver terraform version | awk '{print $2}' | tr -d v)"
  TFOK=$(python3 - "$TFV" <<'PY' 2>/dev/null || echo 1
import sys
try:
    p = [int(x) for x in sys.argv[1].split(".")[:2]]
    print(0 if (p[0], p[1]) >= (1, 7) else 1)
except Exception:
    print(1)
PY
)
  if [ "${TFOK:-1}" -eq 0 ]; then hard terraform 0 "${TFV}" ""
  else hard terraform 1 "${TFV:-不明}（1.7 未満）" "terraform test の mock_provider が動かず make lint が落ちます"; fi
else
  hard terraform 1 "なし" "infra の lint / plan ができません"
fi

# --- 言語ランタイム -----------------------------------------------------------
if has python3; then
  # **文字列パターンで判定しない。** `3.1[23]|3.1[4-9]` のような書き方は
  # 将来の 3.20 を「3.12 未満」と誤って弾く。数値で比べる。
  PYV="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null)"
  PYOK="$(python3 -c 'import sys;print(0 if sys.version_info[:2] >= (3,12) else 1)' 2>/dev/null || echo 1)"
  if [ "${PYOK:-1}" -eq 0 ]; then hard python3 0 "$PYV" ""
  else hard python3 1 "${PYV:-不明}" "3.12 以上が要る（ローカルは 3.13）"; fi
else
  hard python3 1 "なし" "API 側が動きません"
fi
if has node; then
  NV="$(node --version 2>/dev/null | tr -d v | cut -d. -f1)"
  if [ "${NV:-0}" -ge 22 ] 2>/dev/null; then hard node 0 "v${NV}" ""
  else hard node 1 "v${NV:-不明}" "Node 22 以上が要る（依存の engine 要件）"; fi
else
  hard node 1 "なし" "SPA のビルドができません"
fi
# node があっても npm が無い入れ方があるので別に見る（make build / lint が npm を直に呼ぶ）。
has npm && hard npm 0 "$(ver npm --version)" || hard npm 1 "なし" "make build / make lint が動きません"

# --- コンテナエンジン ---------------------------------------------------------
# ops/_container.sh が podman / docker のどちらかを解決する。両方無いと build できない。
if has podman || has docker; then
  eng=$(has podman && echo podman || echo docker)
  hard container 0 "$eng" ""
else
  hard container 1 "なし" "podman か docker のどちらかが要る（ops/_container.sh が解決）"
fi

# --- 設定ファイル -------------------------------------------------------------
# **存在するだけでは足りない。** 雛形を写しただけ（値が空・プレースホルダのまま）でも
# 「ある」と見えてしまい、前提不足を早期に出すという目的を満たさない。
if [ ! -f .env ]; then
  hard ".env" 1 "なし" "cp .env.example .env して実値を入れる"
else
  # **全部の鍵を要求しない。** `AUTH_MODE` や `OCI_PROFILE` は既定があり空で正常。
  # 見るのは「スクリプトが無いと止まる」と宣言している鍵だけ
  # （`${VAR:?...}` と `ops/_adb.py` の `env()` から拾ったもの）。
  ENV_STATE="$(python3 - <<'PYENV'
import re
# `ops/orm-stack.sh` は環境ごとに別の鍵を要求する。片方でも欠けるとその環境が動かない。
REQUIRED = ["COMPARTMENT_OCID", "TENANCY_OCID", "ADB_ADMIN_PASSWORD", "ADB_OCID",
            "INTERNAL_DEV_COMPARTMENT_OCID", "PUBLIC_DEV_COMPARTMENT_OCID"]
placeholder = re.compile(r"^(change-me|<.*>)$|xxxxxxxx")
vals = {}
for line in open(".env", encoding="utf-8", errors="replace"):
    m = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line.rstrip("\n"))
    if m:
        vals[m.group(1)] = m.group(2).strip().strip("\"'")
bad = [k for k in REQUIRED if not vals.get(k) or placeholder.search(vals[k])]
print(("NG " + " ".join(bad)) if bad else ("OK %d" % len([v for v in vals.values() if v])))
PYENV
)"
  case "$ENV_STATE" in
    OK*) hard ".env" 0 "${ENV_STATE#OK } 項目" "" ;;
    *)   hard ".env" 1 "未設定 ${ENV_STATE#NG }" "実値を入れる（雛形のままでは動かない）" ;;
  esac
fi

echo
echo "== あると良い（無くても作業は続けられる）"

has gh && soft gh 0 "$(ver gh --version | awk '{print $3}')" \
       || soft gh 1 "なし" "PR 操作と「止まっている作業」の検出が落ちます"

# **報告の出口はホーム側の個人スキル**（ADR-0018）。無ければ artifact へ退避する設計。
if [ -d "$HOME/.claude/skills/preview" ]; then
  soft preview 0 "あり" ""
else
  soft preview 1 "なし" "報告は artifact へフォールバックします（loop-config: fallback: artifact）"
fi
if [ -f .obsidian-dir ]; then
  soft ".obsidian-dir" 0 "設定済み" ""
else
  soft ".obsidian-dir" 1 "なし" "初回の報告時に出力先を1度だけ確認します"
fi
[ -d .venv ] && soft ".venv" 0 "あり" || soft ".venv" 1 "なし" "python -m venv .venv して依存を入れる"

for c in zip unzip tar jq curl; do
  has "$c" && soft "$c" 0 "あり" || soft "$c" 1 "なし" "一部の ops スクリプトが使います"
done

echo
echo "== 環境の癖（欠落ではないが、踏みやすい）"
BV="${BASH_VERSION%%.*}"
if [ "${BV:-0}" -lt 4 ]; then
  row "note" "bash" "${BASH_VERSION%%(*}" "3.x です。連想配列 / mapfile は使えません（ops は対応済み）"
else
  row "note" "bash" "${BASH_VERSION%%(*}" ""
fi
if date -Is >/dev/null 2>&1; then
  row "note" "date" "GNU" ""
else
  row "note" "date" "BSD" "date -Is は使えません（ops は +%Y-… で対応済み）"
fi
if [ -f "$HOME/.oci/config" ]; then
  R="$(awk -F= '/^region/{gsub(/ /,"",$2);print $2;exit}' "$HOME/.oci/config" 2>/dev/null)"
  row "note" "既定 region" "${R:-未設定}" "--region を渡さない呼び出しはここを向きます"
fi

echo
if [ "$HARD_MISSING" -gt 0 ]; then
  echo "必須が ${HARD_MISSING} 件欠けています。上の「対処」を先に済ませてください。" >&2
  exit 1
fi
if [ "$SOFT_MISSING" -gt 0 ]; then
  echo "必須はすべて揃っています（任意 ${SOFT_MISSING} 件が未設定）。"
else
  echo "すべて揃っています。"
fi
# **どちらの版を触っているかは前提と同じくらい間違えやすい。** doctor を通ったあと
# 最初に見るものとして導線だけ置く（判定そのものは where.sh が持つ）。
echo "いまどちらの版（public / internal）を触っているかは: make where"
