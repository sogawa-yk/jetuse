#!/usr/bin/env bash
# 変更の「起点」が正しいかを検査する（4ブランチ体制 / ADR-0028）。
#
# なぜ要るか（2026-07 の実害）:
#   共有物（docs / specs / 公開アプリコード / ループ機構）を internal 側の枝で作ると、
#   Public へ届かないまま両系統が乖離する。実際 `docs/verification/` を4サブディレクトリへ
#   整理した変更が dev 側だけに入り、main には 105 ファイルが直下に残ったままになった。
#   後追いで main 側 PR を足す羽目になり、以後 sync のたびに手で衝突を解いていた。
#   規律だけでは同じことが起きるので、機械で止める。
#
# 判定:
#   base が internal-dev 以外            → 検査対象外（何もしない）
#   変更が「内部固有パス」を含む         → OK（内部固有の作業とみなす）
#   変更が共有物のみ                     → FAIL（public-dev 起点にやり直す）
#   内部固有と共有物が混在               → WARN（落とさないが、共有部分は main へ届かないと告げる）
#
# 内部固有パスの一覧は ops/internal-only-paths.txt（単一の真実源）。
#
# 使い方:
#   ops/check-branch-base.sh [base-ref]      # 引数が最優先
#   BRANCH_BASE=internal-dev make lint       # ローカルで検査したいとき
#
# base の解決順: 引数 > $BRANCH_BASE > $GITHUB_BASE_REF（PR の base）。
# **どれも無ければ merge-base から推定する。** 以前はここでスキップしていたが、黙って通ると
# 「ローカルでは何も言われない → PR で初めて落ちる」になる（実際そうなった）。
# 判定は **「HEAD の分岐点が public-dev に含まれるか」**:
#   mbi = merge-base(HEAD, internal-dev)
#   mbi が public-dev の祖先 → public-dev 起点 / そうでなければ internal-dev 起点
# **単純な merge-base 等値比較では駄目。** internal-dev が public-dev に追いつく前
# （同期は人間ゲートなので日常的にそうなる）、public-dev 先端から切った枝の
# mb(pub) と mb(int) は食い違い、Public の作業を Internal と誤判定する。
# ただし**推定では落とさない**（`make lint` を壊さない）。警告に留め、強制するのは CI
# （pull_request で $GITHUB_BASE_REF が入る）側のまま。`make where` が同じ推定を使う。
set -euo pipefail
# **呼び出し元の worktree** を見る。$(dirname $0)/.. へ cd すると、ループが使う別 worktree から
# 実行したときにスクリプトの置き場（＝主 worktree）の HEAD を検査してしまう。
cd "$(git rev-parse --show-toplevel)"

PATHS_NAME="ops/internal-only-paths.txt"
BASE="${1:-${BRANCH_BASE:-${GITHUB_BASE_REF:-}}}"

# 推定で決めた base か（真なら FAIL を WARN に落とす）。
ESTIMATED=0
if [ -z "$BASE" ]; then
  _resolve() { for c in "origin/$1" "$1"; do
      git rev-parse --verify --quiet "$c" >/dev/null && { echo "$c"; return 0; }; done; return 1; }
  _pubref="$(_resolve public-dev || true)"
  _intref="$(_resolve internal-dev || true)"
  _cur="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
  case "$_cur" in
    public-dev|main)              BASE=public-dev ;;
    internal-dev|internal-stable) BASE=internal-dev ;;
    *)
      if [ -n "$_pubref" ] && [ -n "$_intref" ]; then
        _mbi="$(git merge-base HEAD "$_intref" 2>/dev/null || true)"
        if [ -n "$_mbi" ]; then
          # **終了値を 0 / 1 / それ以外で分ける。** `--is-ancestor` は「祖先ではない」を 1、
          # 異常（不正な ref・リポジトリ破損など）を 1 より大きい値で返す。まとめて else に
          # 入れると、判定できなかった場合を internal-dev 起点と確定してしまう。
          # `|| _anc=$?` で条件文脈に入れる。裸で書くと `set -e` が非ゼロ終了で
          # スクリプトごと落とし、判定に辿り着かない。
          _anc=0
          git merge-base --is-ancestor "$_mbi" "$_pubref" 2>/dev/null || _anc=$?
          case "$_anc" in
            0) BASE=public-dev ;;
            1) BASE=internal-dev ;;
            *) BASE="" ;;   # 推定不能。下の SKIP へ落ちる
          esac
        fi
      fi
      ;;
  esac
  if [ -z "$BASE" ]; then
    echo "[base] SKIP: base を指定も推定もできません（合格ではない）。" >&2
    echo "[base]       long-lived ブランチを取得すると推定できます: git fetch origin" >&2
    exit 0
  fi
  ESTIMATED=1
  echo "[base] 起点を推定: ${BASE}（明示するなら BRANCH_BASE=... / 詳細は make where）"
fi

BASE="${BASE#refs/heads/}"
if [ "$BASE" != "internal-dev" ]; then
  echo "[base] base=$BASE は検査対象外（internal-dev 宛の PR だけを見る）。"
  exit 0
fi

# base の実体を解決する。CI の checkout は既定で浅いので origin/ 側も見る。
REF=""
for cand in "origin/$BASE" "$BASE"; do
  if git rev-parse --verify --quiet "$cand" >/dev/null; then REF="$cand"; break; fi
done
if [ -z "$REF" ]; then
  # **CI では落とす。** required check が「判断できないので緑」を返すと、履歴取得や
  # checkout 仕様が変わった瞬間に検査が丸ごと省略される（review-6 F002）。
  if [ -n "${GITHUB_BASE_REF:-}" ]; then
    echo "[base] FAIL: CI で base '$BASE' を解決できません（fetch-depth: 0 になっているか確認）。" >&2
    exit 1
  fi
  echo "[base] SKIP: $BASE を解決できない（浅い clone？）。判断できないので検査していません。" >&2
  exit 0
fi

# merge-base が取れないと差分が「全ファイル」になり誤検知するので、取れないならスキップする。
if ! MB=$(git merge-base "$REF" HEAD 2>/dev/null) || [ -z "$MB" ]; then
  if [ -n "${GITHUB_BASE_REF:-}" ]; then
    echo "[base] FAIL: CI で $REF と HEAD の merge-base が取れません（履歴が浅い可能性）。" >&2
    exit 1
  fi
  echo "[base] SKIP: $REF と HEAD の merge-base が取れず検査していません。" >&2
  exit 0
fi

# 分類に使う一覧は **base 側だけ**を読む。
#  - PR 側（HEAD）を混ぜると、検査対象の PR 自身が分類規則を書き換えて迂回できる。
#    共有ファイルの接頭辞を一覧に足せば、その一覧変更ごと Internal 扱いになり素通りした（review-5 F002）。
#  - 削除による迂回も同時に塞がる（review-2 F002）。base 側が常に効くため。
#  - 代償: 新しい内部固有パスは「一覧を先に internal-dev へ入れる」2段階になる。
#    単一の真実源を PR が動かせない方を優先する。
PATTERNS=$(mktemp); trap 'rm -f "$PATTERNS" "${CHANGED:-}"' EXIT
git show "$REF:$PATHS_NAME" > "$PATTERNS" 2>/dev/null || true
if [ ! -s "$PATTERNS" ]; then
  # **base に一覧がまだ無い＝移行前。ここは検査できないので通す。**
  # HEAD 側の一覧で代用してはいけない: 一覧を internal-dev へ運ぶ最初の同期 PR は
  # 「共有物のみ」の差分になり、自分自身が落ちて移行が始められなくなる（review-6 F001）。
  # 迂回の心配も無い —— base に入った後は base 側だけを見るので、PR は規則を弱められない。
  echo "[base] SKIP: base($REF) に $PATHS_NAME がまだ無いため検査できません（合格ではない）。" >&2
  echo "[base]       4ブランチ体制(ADR-0028)を internal-dev へ同期すると有効になります。" >&2
  exit 0
fi

# -z（NUL 区切り）で取る。既定の出力は非 ASCII パスを "..." でクォートして \nnn 展開するため、
# 先頭一致の判定が崩れる（実際 runs/ 配下の日本語ファイル名が中立判定を抜けた）。
# NUL はコマンド置換 $(...) が落とすので、必ずファイル経由で読む。
CHANGED=$(mktemp)
if [ -n "${GITHUB_BASE_REF:-}" ]; then
  # CI: PR の確定差分だけを見る（作業ツリーは存在しない）
  git diff --name-only -z "$MB" HEAD > "$CHANGED"
else
  # ローカル: **未コミット・未追跡も含める。** コミット前チェックとして案内している以上、
  # HEAD までしか見ないと、これから commit する共有物を見逃す（review-3 F002）。
  git diff --name-only -z "$MB" > "$CHANGED"
  git ls-files --others --exclude-standard -z >> "$CHANGED"
fi
[ -s "$CHANGED" ] || { echo "[base] 変更なし。"; exit 0; }

# **正規の同期 PR（public-dev → internal-dev）は検査対象外。**
# 同期ブランチは internal-dev から切って public-dev を merge するので、base 比の差分は
# Public 側の共有物そのものになり、「誤起点の feature PR」と見分けが付かず落ちてしまう
# （review-7 F001）。ブランチ名の規約ではなく**内容**で判別する:
#   internal-dev にも public-dev にも無い「独自の非 merge コミット」が1つも無ければ、
#   それは Public の内容を運んでいるだけの同期。feature branch は必ず独自コミットを持つ。
for _pub in "origin/public-dev" "public-dev"; do
  git rev-parse --verify --quiet "$_pub" >/dev/null || continue
  if git merge-base --is-ancestor "$_pub" HEAD 2>/dev/null \
     && [ -z "$(git rev-list --no-merges "$REF".."HEAD" --not "$_pub" 2>/dev/null)" ]; then
    echo "[base] OK: $_pub を運ぶ同期 PR（独自コミット無し）。起点検査の対象外。"
    exit 0
  fi
  break
done

# ループの実行履歴だけは「どちらの版のものか」を示さないので判定材料から外す。
# **これを広げすぎないこと。** 当初 docs/verification/ と tasks/ も中立にしていたが、
# 再発防止の対象そのもの（2026-07 の verification/ 整理が Public に届かなかった件）が
# 素通りしていた（review-2 F001）。共有ドキュメントは既定で shared とし、Internal 固有の
# ものだけ internal-only-paths.txt に列挙する。
is_neutral() {
  case "$1" in
    runs/*) return 0 ;;
    *) return 1 ;;
  esac
}

is_internal_only() {
  local f="$1" pat
  while IFS= read -r pat; do
    pat="${pat%%#*}"; pat="${pat#"${pat%%[![:space:]]*}"}"; pat="${pat%"${pat##*[![:space:]]}"}"
    [ -n "$pat" ] || continue
    case "$f" in "$pat"*) return 0 ;; esac
  done < "$PATTERNS"
  return 1
}

internal=0; shared=0; shared_list=""
while IFS= read -r -d '' f; do
  [ -n "$f" ] || continue
  # **内部固有の明示指定を neutral より先に見る。** 逆順にすると、一覧に載っている
  # docs/verification/demo-platform/ が中立扱いになり internal に数えられず、
  # 正当な混在 PR が「共有物のみ」と誤判定される（review-1 F003）。
  if is_internal_only "$f"; then
    internal=$((internal + 1))
  elif is_neutral "$f"; then
    continue
  else
    shared=$((shared + 1))
    # 一覧は先頭 20 件で打ち切る（CI ログを埋めないため）。件数は $shared が持つ。
    [ "$shared" -le 20 ] && shared_list="${shared_list}    $f"$'\n'
  fi
done < "$CHANGED"
[ "$shared" -gt 20 ] && shared_list="${shared_list}    …ほか $((shared - 20)) 件"$'\n'

if [ "$shared" -eq 0 ]; then
  echo "[base] OK（内部固有 $internal 件 / 共有物 0 件）"
  exit 0
fi

if [ "$internal" -gt 0 ]; then
  echo "[base] WARN: 内部固有 $internal 件と共有物 $shared 件が混在しています。" >&2
  echo "[base] 共有部分は internal-dev に入れても main へ届きません。分割できるなら分けてください:" >&2
  printf '%s' "$shared_list" >&2
  exit 0
fi

_LVL=FAIL
[ "$ESTIMATED" = 1 ] && _LVL=WARN
cat >&2 <<EOF
[base] ${_LVL}: 共有物しか変更していないのに起点が internal-dev です（$shared 件）。

$(printf '%s' "$shared_list")
  共有物を internal 側の枝に入れると Public へ届かず、両系統が乖離します。
  base を public-dev にしてください（最新 public-dev から切り直して cherry-pick するのが確実）。

  内部固有のつもりなら、そのパスを $PATHS_NAME に追加してください。
  ただし「Public 版に出しても差し支えないか」を先に考えること。差し支えないなら
  一覧に足すのではなく public-dev 起点で作り直すのが正しい対応です。
EOF
# **推定では落とさない。** 推定は信頼できるが明示された base ではないので、`make lint` を
# 止める根拠にはしない。この起点のまま PR を出せば CI が同じ判定で落とす。
if [ "$ESTIMATED" = 1 ]; then
  echo "[base] （推定のため lint は通します。この起点で PR を出すと CI が落とします）" >&2
  exit 0
fi
exit 1
