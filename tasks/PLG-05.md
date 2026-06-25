# タスク: PLG-05 公開フロー（builder → export → 署名 → publish）

## ゴール
インスタンスで作った usecase/agent 定義をマーケットへ公開する導線を実装する（D7）。

## 対象 area
both（api ＋ web）

## 依存
PLG-01, PLG-04

## 仕様参照
docs/enhance/202607-demo-platform-plan.md §6 / specs/16-platform.md

## 受け入れ条件（検証可能な述語で書く）
- [ ] 既存 UC/Agent 定義を manifest 化する export を実装
- [ ] 発行者鍵で manifest に署名する
- [ ] publish API を呼び出して公開する
- [ ] builder.tsx / agentbuilder.tsx に「マーケットに公開」アクションを追加
- [ ] builder からテストUCを公開→レジストリの list に出現する、を実機E2Eで確認し docs/verification/PLG-05.md に記録
- [ ] `npm --prefix packages/web run build` 成功・eslint クリーン

## 成果物
export/署名ロジック / UI 改修 / docs/verification/PLG-05.md

## 非ゴール / 制約
- 審査キューは導入しない（D7=署名付き直接公開）。
- 発行者鍵は .env / Vault 管理。鍵実値をコミットしない。コミット/PR/push は人間承認後。
