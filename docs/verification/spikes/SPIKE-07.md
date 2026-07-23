# SPIKE-07 (UI-SPIKE): Redwood風デザインシステム試作

実施日: 2026-06-10 / 成果物: `packages/web/`（Vite + React 19 + TypeScript + Tailwind v4）

## 目的

Redwood風デザイントークンで主要コンポーネントのギャラリーを作成し、人間レビューで「Oracleっぽさ」のルック&フィール承認を得る。branding.json差し替えデモを含む。並行してJetUse（MIT-0）フロントの流用可能部品を特定する。

## 実装内容

### デザイントークン（`src/theme.css`）

- CSS変数2層構造: `--brand-*`（branding.jsonで実行時上書き可能なブランド色）と `--rw-*`（Redwood風ニュートラル）→ Tailwind v4の `@theme inline` でユーティリティ化
- プライマリ `#C74634`（Oracle Red近傍）、背景はクリーム〜グレージュ（`#f5f4f2` / `#fcfbfa`）、細罫線 `#d9d4cf`、控えめグリーンアクセント
- フラット基調: 角丸4px・影は1pxの最小限。ダークモードは `.dark` クラスでトークン一括切替
- フォント: Inter + Noto Sans JP（Google Fonts、Oracle Sans不使用）

### ギャラリー（`npm run build` 成功・eslintクリーン）

左ナビ（ユースケース一覧+会話履歴、JetUse踏襲レイアウト）/ チャットバブル（ストリーミング表示アニメデモ・コピー操作付き）/ フォーム部品（text・select・textarea・スライダー=動的フォームの想定部品）/ テーブル（NL2SQL結果想定）/ トースト / ボタン5種 / ダークモード切替

### branding.json差し替えデモ

`public/branding.json`（プロダクト名・ロゴ文字・カラー3色）を実行時fetchしてCSS変数とタイトルに反映。ギャラリー上部の「リブランド例」ボタンで青系ブランド（`branding-custom-example.json`）への切替を実演 → **コード変更ゼロでのリブランドが可能なことを確認**。

## レビュー方法（人間チェックポイント①の一部）

インスタンス上でプレビューサーバー起動済み（port 4173）。手元のマシンから:

```bash
ssh -L 4173:localhost:4173 opc@<このインスタンスのIP>
# ブラウザで http://localhost:4173
```

確認観点: ①Redwoodの公式デモ/OCIコンソールと並べた「Oracleっぽさ」 ②ダークモード ③リブランド切替

## JetUseフロント流用調査（MIT-0、ロジックのみ流用・見た目は流用しない）

AWS依存は (a)Cognito認証 (b)Lambdaレスポンスストリーム (c)S3署名URL の3点に局所化されており、以下はほぼそのまま移植可能:

| 部品 | JetUseのファイル | 流用方針 |
|---|---|---|
| Markdownレンダラ | `components/Markdown.tsx` | react-markdown@9 + remark-gfm/breaks/math + rehype-katex + PrismLight（20言語個別登録）。S3 URL解決のみOCI PARに差替で**ほぼそのまま** |
| Mermaid表示 | `components/Mermaid/MermaidWithToggle.tsx` | mermaid@11。**ストリーミング中はコード表示、500ms変化なしで図に自動切替**するデバウンス設計が秀逸。AWS依存ゼロで**そのまま** |
| ストリーミング受信 | `hooks/useChat.ts`(L606-720) + `utils/streamParser.ts` | NDJSONチャンク分割対応バッファ + 10文字バッファでstore反映の設計を踏襲。トランスポートだけSSE/fetchに**書き直し** |
| タイピング表示 | `hooks/useTyping.ts` | 残量に応じ表示速度可変。**そのまま** |
| 自動スクロール追従 | `hooks/useFollow.ts` | ResizeObserver+手動スクロールで解除。**そのまま** |
| 動的フォーム生成 | `utils/UseCaseBuilderUtils.ts` | `{{inputType:label:options}}` プレースホルダのパース。依存ゼロで**そのまま**（UC-01の心臓部に直結） |
| チャットstore | `hooks/useChat.ts` | zustand+immer、ページIDキー辞書 + chunk追記アクションの構造を踏襲。永続化API部は**書き直し** |
| コピー/アップロード/i18n | `ButtonCopy.tsx` / `useFiles.ts` / `i18n/config.ts` | i18next+YAML辞書（ja/en翻訳資産も再利用価値大）。**ほぼそのまま** |

## 設計への影響

- `specs/01-design-system.md` を本ギャラリーの実装で具体化（人間承認後に確定版へ更新）
- Phase 2以降の追加依存が確定: react-markdown系一式、mermaid、zustand、immer、i18next、sonner相当のトースト
- UC-01（テンプレートエンジン）はJetUseのプレースホルダ記法 `{{inputType:label:options}}` と互換にすると移植コスト最小

## 残課題

- 人間レビューによるルック&フィール承認（承認後 specs/01 確定）
- Oracle JET不採用のADR正式化（本スパイクの生産性実証を根拠として記載予定）
- コンポーネントのStorybook化はPhase 1（APP-02）で判断
