#!/usr/bin/env bash
# 報告書 HTML を実際にレンダリングして、目視確認用の PNG を出す。
#
# なぜ必要か: 報告書は「書けた」ではなく「読めた」で完了する。座標手置きの図の重なり・
#   狭い幅での見切れ・不自然な折り返しは、HTML を読んでも分からずレンダリングして初めて出る
#   （2026-07-28: 図が重なった報告書をそのまま提示して差し戻された）。
#
# 使い方: check_report_render.sh <報告書.html> [出力先ディレクトリ]
#   出力: <出力先>/<名前>-render-700.png（Obsidian 埋め込み相当の幅）
#         <出力先>/<名前>-render-430.png（縦積み時の確認）
#   出した PNG は Read ツールで**実際に開いて確認する**（report-style.md のチェックリスト）。
#
# 終了コード: 0=撮れた / 2=引数不正 / 3=Chrome が無い（検証スキップ。報告に明記すること）
set -euo pipefail

HTML="${1:?usage: check_report_render.sh <report.html> [outdir]}"
[ -f "$HTML" ] || { echo "[report] ファイルが無い: $HTML" >&2; exit 2; }

ABS="$(cd "$(dirname "$HTML")" && pwd)/$(basename "$HTML")"
OUT="${2:-$(dirname "$ABS")}"
BASE="$(basename "${ABS%.html}")"
CH="${CHROME_BIN:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"

if [ ! -x "$CH" ]; then
  echo "[report] Chrome が見つからない: ${CH}（CHROME_BIN で指定可）。" >&2
  echo "[report] レンダリング確認をスキップした旨を報告に明記すること。" >&2
  exit 3
fi

mkdir -p "$OUT"
for W in 700 430; do
  PNG="${OUT}/${BASE}-render-${W}.png"
  "$CH" --headless --disable-gpu --hide-scrollbars \
    --screenshot="$PNG" --window-size="${W},3000" "file://${ABS}" >/dev/null 2>&1
  echo "$PNG"
done
