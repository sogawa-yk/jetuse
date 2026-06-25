# タスク: SBA-02 AI組込フレームワーク + コアアプリ SBA-A「問い合わせ/サポート管理」(RAG)

## ゴール
「業務アプリ＋AI」を実証し、以降のサンプルアプリの型（AI組込スロットの実行時バインド）を確立する。

## 対象 area
both（api ＋ web）

## 依存
SBA-01, PLG-07

## 仕様参照
docs/enhance/202607-demo-platform-plan.md §6（SBA-A）/ specs/16-platform.md

## 受け入れ条件（検証可能な述語で書く）
- [ ] AI組込スロットの実行時バインド機構を実装
- [ ] コア同梱 sample-app SBA-A（テンプレUI＋FAQシードデータ）を用意
- [ ] RAG回答・自動分類・要約・返信ドラフトを SBA-A の組込点に配置
- [ ] home・実行導線から SBA-A を起動できる
- [ ] SBA-A デモが実環境で起動し、FAQ に対する RAG 回答が動く、を docs/verification/SBA-02.md に記録
- [ ] `npm --prefix packages/web run build` / vitest / `.venv/bin/pytest` / lint がパス

## 成果物
AI組込FW / sample-app SBA-A 定義＋シード / UI / docs/verification/SBA-02.md

## 非ゴール / 制約
- 残り3本（SBA-B/C/D）は SBA-03..05。コネクタ連携はステージ3。
- 既存リソースは参照のみ。人間ゲート: デモ品質チェック。コミット/PR/push は人間承認後。
