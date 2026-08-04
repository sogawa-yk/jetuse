#!/usr/bin/env bash
# 実 OCID（完全長）がコミット対象に混ざっていないかを検査する（ADR-0029）。
#
# なぜ要るか（2026-08-04 の棚卸し）:
#   このリポジトリは public。runs/ の証跡には OCID が約6,700回出現するが、大半は
#   `ocid1.tenancy.oc1..MASKED` の形にマスクされている。手動のマスク規律はよく効いているが、
#   追跡対象を機械で洗うと取りこぼしが10個あり、うち tenancy 1・compartment 3 だった
#   （手作業のスキャンでは9個・tenancy/compartment 無しと誤って結論していた）。
#   .gitignore による除外では防げない経路にも入っていた:
#     - 証跡として残す側のファイル（runs/.../e2e/DONE.md）
#     - runs/ の外（docs/archive/spikes/spike14b_aisdk.mjs に compartment の実値）
#   コミット前に機械で止める。
#
# 危険度は種別で変わる:
#   ormjob / ormstack / generativeaiproject 等 = リソースの存在が分かるだけ。認証情報ではない
#   tenancy / compartment                      = サポート詐称や cross-tenancy ポリシーの標的化に使われうる
#   → 前者だけ allowlist で受容できる。後者は**コードで受容を拒否する**（運用規律に頼らない）。
#
# 検査対象:
#   既定       = index（ステージ済み）＋ 追跡ファイルの作業ツリー内容
#                （どちらか一方だと「stage してから作業ツリーだけマスク」で抜けられる）
#   --all      = 上記＋未追跡ファイル
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

ALLOW="ops/allowed-public-ocids.txt"
SCAN_ALL=0
for a in "$@"; do case "$a" in --all) SCAN_ALL=1 ;; *) echo "unknown flag: $a" >&2; exit 2 ;; esac; done

# 完全長 = 実値。マスク済み（...MASKED / 短縮形）は長い本体を持たない。
# OCID の構造: ocid1.<type>.<realm>[.<region>][.<future-use>].<unique-id>
#   - realm は oc1 に限らない（OC2/OC3/OC4 等）。固定すると別 realm を見逃す
#   - region と future-use は有無も個数も一定しないので、中間セグメントは緩く受ける
#   - unique-id の長さはリソース依存。閾値は 30 にした（MASKED 等の伏字は十分短い）
# **これは正規表現による近似であり、全形式の網羅は保証しない**（residual: ADR-0029）。
PAT='ocid1\.[a-z0-9]+\.oc[0-9]+\.[a-z0-9.-]*\.[a-z0-9]{30,}'
# 受容してはいけない種別。allowlist に書かれていても落とす。
# **行頭に固定しないこと。** `^` で縛ると `# ocid1.tenancy...` のようにコメント化したり
# 先頭に空白を入れるだけで、allowlist はスキャン対象外なので検査を丸ごと迂回できた
# （review-17 blocker）。完全長の本体まで含めて照合するので、マスク例の記載は誤検出しない。
# 閾値と中間セグメントの扱いは PAT と必ず揃える。緩い方(PAT)で「実値」と判定されるのに
# 厳しい方(NEVER_ALLOW)で拾えないと、その隙間の tenancy/compartment を受容できてしまう。
NEVER_ALLOW='ocid1\.(tenancy|compartment)\.oc[0-9]+\.[a-z0-9.-]*\.[a-z0-9]{30,}'

HITS=$(mktemp); ALLOWED=$(mktemp)
trap 'rm -f "$HITS" "$ALLOWED"' EXIT

# **オプションはパターンより前に置く。** 後ろに置くと git が revision と解釈して
# `fatal: unable to resolve revision: --untracked` になり、|| true で握り潰されて
# 「検出なし」で素通りする（検査が黙って無効化される）。パターンは -e で渡す。
# **allowlist の妥当性検査は走査より前に行う。** 後ろに置くと「検出0件」の早期 return が
# 先に走り、allowlist に紛れ込ませた tenancy / compartment を素通りさせる（review-17 blocker）。
if [ -f "$ALLOW" ]; then
  # ファイル全体（コメント含む）を見る。位置に関係なく tenancy / compartment を拒否する。
  if grep -qE "$NEVER_ALLOW" "$ALLOW" 2>/dev/null; then
    echo "[ocid] FAIL: $ALLOW に tenancy / compartment の実 OCID が書かれています。" >&2
    echo "[ocid]       この2種は受容しません（コメント行でも不可）。値をマスクしてください。" >&2
    exit 1
  fi
  # 受容エントリ: コメントと空行を落とし、前後の空白を除く
  sed -e 's/[[:space:]]*#.*$//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' "$ALLOW" \
    | grep -vE '^$' > "$ALLOWED" || true
fi

EXCL=':(exclude)'"$ALLOW"
# **`|| true` で握り潰さない。** git grep の終了コードは 1=一致なし / 2以上=エラー。
# 一律に無視すると、オプション非対応・index 破損・pathspec エラーでも空の結果になり
# `[ocid] OK` を返す（fail-open）。過去2回この壊れ方を作っているので、エラーは落とす。
scan() {  # $@ = git grep への追加オプション
  local rc=0
  git grep -I -h -o -E "$@" -e "$PAT" -- . "$EXCL" || rc=$?
  if [ "$rc" -ge 2 ]; then
    echo "[ocid] FAIL: git grep がエラー終了しました（rc=$rc, opts=$*）。検査できていません。" >&2
    exit 1
  fi
}
{
  scan --cached          # index（ステージ済みの内容そのもの）
  scan                   # 追跡ファイルの作業ツリー内容
  if [ "$SCAN_ALL" = 1 ]; then scan --untracked; fi
} | sort -u > "$HITS"

[ -s "$HITS" ] || { echo "[ocid] OK（完全長 OCID の混入なし）"; exit 0; }

# 受容済み。ただし tenancy / compartment は allowlist にあってもコードで拒否する。
# **`$(cmd1 && cmd2 || cmd3)` を使わないこと。** 全件が allowlist に載ると grep は
# 「一致なし」で exit 1 を返し、|| 側の cat が走って**全件を新規混入として報告する**。
if [ -s "$ALLOWED" ]; then
  NEW=$(grep -vxF -f "$ALLOWED" "$HITS" || true)
else
  NEW=$(cat "$HITS")
fi
[ -n "$NEW" ] || { echo "[ocid] OK（検出はすべて $ALLOW で受容済み）"; exit 0; }

echo "[ocid] FAIL: 受容していない実 OCID が $(printf '%s\n' "$NEW" | grep -c . ) 件あります。" >&2
printf '%s\n' "$NEW" | while IFS= read -r o; do
  [ -n "$o" ] || continue
  echo "  $(printf '%s' "$o" | cut -c1-40)…" >&2
  # index / 未追跡が出所のこともあるので3経路とも探す（作業ツリーだけだと出所不明になる）
  { git grep -I -l -e "$o" -- . 2>/dev/null || true
    git grep -I -l --cached -e "$o" -- . 2>/dev/null || true
    if [ "$SCAN_ALL" = 1 ]; then git grep -I -l --untracked -e "$o" -- . 2>/dev/null || true; fi
  } | sort -u | sed 's/^/      /' | head -3 >&2
done
cat >&2 <<EOF

  このリポジトリは public です。実 OCID をマスクしてください（例: ocid1.tenancy.oc1..MASKED）。
  既に公開済みで受容すると決めたものだけ $ALLOW に追加します。
  **tenancy / compartment は受容できません**（危険度が変わるため）。必ずマスクしてください。
EOF
exit 1
