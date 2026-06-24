# 目的
OCIコンソール風の管理画面UIを作成する。


レイアウトの正は添付スクリーンショット（`docs/ui/screen-example-1.png`, `docs/ui/screen-example-2.png`）。色・タイポグラフィ・スペーシング等の
トークンの正は Redwood Design System(下記手順で抽出)。
両者が矛盾する場合はスクリーンショットを優先する。

# Phase 1: Redwood トークン抽出
1. 作業用に `npm install @oracle/oraclejet` を実行する。
2. `node_modules/@oracle/oraclejet/dist/css/redwood/oj-redwood.css` が対象ファイル。
   ※ 約1.1MBあるため、このファイルを直接読み込まないこと。
   必ず Node.js の抽出スクリプトを書いて処理する。
3. 抽出スクリプトの要件:
   - `--oj-*: 値;` 形式の変数定義をすべて収集する
   - 重要: 同名変数がライトテーマとダークテーマで複数回定義されている。
     ファイル内の「最初の定義」をライトテーマ、2回目以降の再定義は
     ダークテーマとして別管理すること(後勝ちで上書きしない)
   - `rgb(var(--oj-palette-neutral-rgb-0))` のような多段参照を再帰的に
     実値(hex または rgb)へ解決すること
   - 出力は2ファイル:
     - `src/styles/tokens.css` … :root にライトテーマ、
       [data-theme="dark"] にダークテーマの解決済み変数
     - `tokens-report.md` … 主要トークン(brand色、neutralパレット、
       text/bg色、border-radius、box-shadow、typography一式)の一覧表
4. tokens-report.md を提示して私の確認を待つこと。
   特に neutral-170(#312D2A 系)がヘッダー用ダーク色として
   抽出できているかを確認ポイントとする。

# Phase 2: 実装
- フォントは Oracle Sans を使わず、抽出された
  -apple-system 始まりのシステムフォントスタックを使う
- ハードコードの色値は禁止。必ず tokens.css の変数を参照する
- コンポーネント分解(ヘッダー / サイドナビ / パンくず / テーブル等)を
  先に提示してから実装に入る

# Phase 3: 自己検証
- Playwright で自分の成果物のスクリーンショットを撮り、
  添付画像と比較してズレを列挙 → 修正。このループは最大2周まで。

# 禁止事項
- oj-redwood.css 全文の読み込み・コンテキストへの貼り付け
- redwood.oracle.com への fetch/curl(SPAのため取得不可)
- 本物のOCIコンソールへの操作