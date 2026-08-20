#!/usr/bin/env bash
# **いまどちらの版を触っているのか**を1画面で出す（ADR-0028 / ADR-0031）。
#
# なぜ要るか: 「公開版か内部版か」を言い忘れたまま作業が進むと、共有物が internal 側に
# 着地して `main` へ届かない（2026-07 の実害）。従来この検査は CI（PR の base）でしか
# 効かず、ローカルでは「知る術が無い」として**黙ってスキップ**していた。
#
# 起点は推定できる。判定は **「HEAD の分岐点が public-dev に含まれるか」**:
#   mbi = merge-base(HEAD, internal-dev)
#   mbi が public-dev の祖先        → 分岐点は Public 側にもある = public 起点
#   そうでない                       → 分岐点が internal 固有のコミットを含む = internal 起点
#
# **単純な merge-base 等値比較では駄目**（Codex 指摘の blocker）。internal-dev が
# public-dev に追いつく前（同期は人間ゲートなので日常的にそうなる）、public-dev 先端から
# 切った枝は mb(pub)=先端 / mb(int)=同期済みの古い点 となって食い違い、Public の作業を
# Internal と誤判定して配備先まで間違える。
#
# 限界: 枝を切った位置が public-dev にも存在するコミットなら、internal-dev から切っていても
# public 起点と区別できない（merge-base が一致するため）。実害は小さい —— その位置は
# public-dev にもあるので共有物を入れる先として public-dev は正しい。**推定であって宣言ではない。**
# 強制するのは CI（PR の base が明示される）側で、ここは取り違えに気づかせるための表示。
#
# 使い方: make where  /  ops/where.sh
set -uo pipefail
cd "$(git rev-parse --show-toplevel)"

BR="$(git rev-parse --abbrev-ref HEAD)"
row() { printf '  %-14s %s\n' "$1" "$2"; }

# --- 起点 ---------------------------------------------------------------------
have() { git rev-parse --verify --quiet "$1" >/dev/null; }
REF_PUB=""; REF_INT=""
for c in origin/public-dev public-dev; do have "$c" && { REF_PUB="$c"; break; }; done
for c in origin/internal-dev internal-dev; do have "$c" && { REF_INT="$c"; break; }; done

VERSION="不明"; BASE="不明"; WHY=""
if [ -z "$REF_PUB" ] || [ -z "$REF_INT" ]; then
  WHY="長期ブランチを解決できない（git fetch が要る）"
elif [ "$BR" = "public-dev" ] || [ "$BR" = "main" ]; then
  VERSION="public（公開版）"; BASE="$BR"; WHY="長期ブランチ上にいる"
elif [ "$BR" = "internal-dev" ] || [ "$BR" = "internal-stable" ]; then
  VERSION="internal（内部版）"; BASE="$BR"; WHY="長期ブランチ上にいる"
else
  MBI="$(git merge-base HEAD "$REF_INT" 2>/dev/null || true)"
  if [ -z "$MBI" ]; then
    WHY="merge-base が取れない（履歴が浅い可能性）"
  else
    # **終了値を 0 / 1 / それ以外で分ける。** `--is-ancestor` は「祖先ではない」を 1、
    # 異常（不正な ref・リポジトリ破損など）を 1 より大きい値で返す。まとめて else に
    # 入れると、答えを出せなかった場合を internal と言い切ってしまう（fail-closed 違反）。
    # `|| _anc=$?` で条件文脈に入れる（`set -e` が足されても落ちないように）。
    _anc=0
    git merge-base --is-ancestor "$MBI" "$REF_PUB" 2>/dev/null || _anc=$?
    case "$_anc" in
      0) VERSION="public（公開版）"; BASE="public-dev"
         WHY="分岐点が public-dev にも含まれる" ;;
      1) VERSION="internal（内部版）"; BASE="internal-dev"
         WHY="分岐点が internal 固有のコミットを含む" ;;
      *) WHY="祖先関係を判定できない（git が ${_anc} で終了）" ;;
    esac
  fi
fi

echo "== いまの作業"
row "ブランチ" "$BR"
row "版" "$VERSION"
row "起点" "${BASE}${WHY:+  (${WHY})}"

# --- 変更内容から見た「あるべき起点」 ------------------------------------------
# 判定の正本は ops/internal-only-paths.txt。**信頼できる長期ブランチ側から読む。**
#   - 作業ツリー側を読むと、その枝が一覧に共有パスを足すだけで「整合」を偽装できる
#     （check-branch-base.sh が同じ理由で base 側だけを見ている）。
#   - **public-dev / internal-dev の両方を読んで合併する。** 新しい内部固有パスは
#     「先に internal-dev の一覧へ入れる」2段階運用なので、public 起点のときに
#     public-dev の一覧だけを見ると、登録済みの内部固有パスを共有物と誤分類して
#     「合っています」と言ってしまう。どちらも枝からは書き換えられないので合併して安全。
#
# **分類できなかったときは必ずそう言う。** 黙ってこの節を省くと、取り違えているのに
# 何も出ない画面と区別が付かない（fail-closed）。
PATHS_NAME="ops/internal-only-paths.txt"
PATHS="$(mktemp)"; CHANGED_F="$(mktemp)"
trap 'rm -f "${PATHS:-}" "${CHANGED_F:-}"' EXIT

CLASSIFY_ERR=""
if [ -z "$REF_PUB" ] || [ -z "$REF_INT" ]; then
  CLASSIFY_ERR="長期ブランチを解決できない（git fetch origin）"
elif [ "$BASE" = "不明" ]; then
  CLASSIFY_ERR="起点が判らない"
else
  CMP_REF="$REF_PUB"; [ "$BASE" = "internal-dev" ] && CMP_REF="$REF_INT"
  if ! MB="$(git merge-base HEAD "$CMP_REF" 2>/dev/null)" || [ -z "$MB" ]; then
    CLASSIFY_ERR="${CMP_REF} との merge-base が取れない（履歴が浅い可能性）"
  else
    # **片方でも読めなければ止める。** 読めた側だけで分類を続けると union が欠けたまま
    # 「合っています」と言える（internal-dev にだけ登録済みの内部固有パスが共有物になる）。
    for _ref in "$REF_PUB" "$REF_INT"; do
      if ! git show "${_ref}:${PATHS_NAME}" >> "$PATHS" 2>/dev/null; then
        CLASSIFY_ERR="${_ref} から ${PATHS_NAME} を読めない"
        break
      fi
    done
    if [ -n "$CLASSIFY_ERR" ]; then
      :
    elif [ ! -s "$PATHS" ]; then
      CLASSIFY_ERR="${PATHS_NAME} が空"
    elif ! git diff --name-only -z "$MB" > "$CHANGED_F" 2>/dev/null; then
      CLASSIFY_ERR="git diff に失敗"
    elif ! git ls-files --others --exclude-standard -z >> "$CHANGED_F" 2>/dev/null; then
      CLASSIFY_ERR="git ls-files に失敗"
    fi
  fi
fi

echo
if [ -n "$CLASSIFY_ERR" ]; then
  echo "== 変更"
  row "分類" "できませんでした（${CLASSIFY_ERR}）"
elif [ ! -s "$CHANGED_F" ]; then
  echo "== 変更"
  row "差分" "なし（起点と同じ）"
else
  # **-z（NUL 区切り）で渡す。** 既定の出力は非 ASCII パスを "..." で括って \nnn 展開するため
  # 先頭一致の判定が崩れる（runs/ 配下の日本語ファイル名で実際に起きた）。改行を含む名前も
  # 割れる。NUL はコマンド置換が落とすので必ずファイル経由で渡す。
  RESULT="$(CHANGED_F="$CHANGED_F" PATHS="$PATHS" python3 - <<'PY' 2>/dev/null || true
import os
pats = [ln.strip() for ln in open(os.environ["PATHS"], encoding="utf-8")
        if ln.strip() and not ln.startswith("#")]
raw = open(os.environ["CHANGED_F"], "rb").read().decode("utf-8", "surrogateescape")
files = sorted({f for f in raw.split("\0") if f.strip()})
internal = [f for f in files if any(f.startswith(p) for p in pats)]
shared = [f for f in files if f not in internal]
print(f"{len(files)} {len(internal)} {len(shared)}")
print("\n".join(internal[:3]))
PY
)"
  NF=""; NI=""; NS=""
  read -r NF NI NS <<< "$(printf '%s' "$RESULT" | head -1)"
  SAMPLE="$(printf '%s' "$RESULT" | tail -n +2 | tr '\n' ' ')"
  if [ -z "$NF" ] || [ -z "$NI" ] || [ -z "$NS" ]; then
    echo "== 変更"
    row "分類" "できませんでした（python3 が要ります）"
  else
    echo "== 変更（起点からの差分 ${NF} ファイル）"
    row "内部固有" "${NI} 件${SAMPLE:+  例: ${SAMPLE}}"
    row "共有物" "${NS} 件"
    echo
    if [ "$NI" -eq 0 ] && [ "$BASE" = "internal-dev" ]; then
      echo "  !! 共有物しか変更していないのに internal-dev 起点です。" >&2
      echo "     このままだと main へ届きません。public-dev から切り直してください。" >&2
      echo "     （CI の branch-base 検査も同じ理由で落とします）" >&2
    elif [ "$NI" -gt 0 ] && [ "$BASE" = "public-dev" ]; then
      echo "  !! 内部固有パスを触っているのに public-dev 起点です。" >&2
      echo "     内部固有のものは Public 版に存在しません。internal-dev 起点にしてください。" >&2
    elif [ "$NI" -gt 0 ] && [ "$NS" -gt 0 ]; then
      echo "  注意: 内部固有と共有物が混在しています。共有部分は main へ届きません。"
    else
      echo "  起点と変更内容は合っています。"
    fi
  fi
fi

# --- 配備先 -------------------------------------------------------------------
echo
echo "== この版の配備先"
# **`.env` は source しない**（このリポジトリの慣習。任意のコードを実行しないため）。
# 優先順: 環境変数 > .env の値。
env_val() {
  eval "_v=\${$1:-}"
  if [ -n "${_v:-}" ]; then printf '%s' "$_v"; return; fi
  [ -f .env ] || return
  sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" .env | tail -1 \
    | sed -e 's/^["'"'"']//' -e 's/["'"'"']$//'
}
case "$VERSION" in
  public*)   ENVNAME=public-dev;   COMP="$(env_val PUBLIC_DEV_COMPARTMENT_OCID)";   PREFIX=jetuse-pubdev
             ENVNAME_VAR=PUBLIC_DEV_COMPARTMENT_OCID ;;
  internal*) ENVNAME=internal-dev; COMP="$(env_val INTERNAL_DEV_COMPARTMENT_OCID)"; PREFIX=jetuse-dev
             ENVNAME_VAR=INTERNAL_DEV_COMPARTMENT_OCID ;;
  *)         ENVNAME=""; COMP=""; PREFIX=""; ENVNAME_VAR="" ;;
esac
if [ -z "$ENVNAME" ]; then
  row "コンパートメント" "版が判らないため未解決"
else
  row "コンパートメント" "jetuse:${ENVNAME}"
  row "資源の接頭辞" "$PREFIX"
  row "ORM スタック" "jetuse-${ENVNAME}-foundation"
  ORM_REG="$(env_val ORM_REGION)"; ORM_REG="${ORM_REG:-us-chicago-1}"
  if [ -z "$COMP" ]; then
    row "ADB" "確認できない（.env の ${ENVNAME_VAR} が未設定）"
  elif ! command -v oci >/dev/null 2>&1; then
    row "ADB" "確認できない（oci CLI が無い）"
  else
    ST="$(oci db autonomous-database list -c "$COMP" --region "$ORM_REG" \
          --query "data[?\"display-name\"=='${PREFIX}-adb'].\"lifecycle-state\" | [0]" \
          --raw-output 2>/dev/null || true)"
    ST="$(printf '%s' "$ST" | tr -d '[:space:]')"
    case "$ST" in
      AVAILABLE) row "ADB" "AVAILABLE" ;;
      "")        row "ADB" "確認できない（認証・通信）" ;;
      *)         row "ADB" "$ST  → ops/start-adb-if-stopped.sh ${ENVNAME}" ;;
    esac
  fi
fi
