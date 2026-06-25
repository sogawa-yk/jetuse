# タスク: PLG-06 マーケットプレイス UI

## ゴール
アプリ内マーケットで一覧・検索・詳細・install/uninstall・更新管理ができるようにする。

## 対象 area
web（＋ api ルート微修正）

## 依存
PLG-03, PLG-04

## 仕様参照
docs/enhance/202607-demo-platform-plan.md §6 / specs/16-platform.md

## 受け入れ条件（検証可能な述語で書く）
- [ ] /marketplace ページ（一覧・検索・タグ・詳細）を実装
- [ ] install / uninstall ボタンが PLG-03 のロジックを（API経由で）呼ぶ
- [ ] インストール済み・更新あり（版比較）を表示
- [ ] 左ナビにマーケット導線を追加
- [ ] 一覧→詳細→install→home に出現→uninstall までUIで通る、を docs/verification/PLG-06.md に記録
- [ ] `npm --prefix packages/web run build` ・ `npm --prefix packages/web run test` ・ eslint がパス

## 成果物
marketplace.tsx ＋ コンポーネント / api ルート / docs/verification/PLG-06.md

## 非ゴール / 制約
- 評価・レビュー・DL数表示はステージ4。
- 既存デザイントークン（Redwood）に準拠、ハードコード色禁止。コミット/PR/push は人間承認後。
