# タスク: SBA-05 コアアプリ SBA-D「帳票・経費処理」(マルチモーダルOCR)

## ゴール
帳票画像を VLM-OCR で読み取り、項目抽出・検証・登録するワークフローのデモを用意する。

## 対象 area
both（api ＋ web）

## 依存
SBA-02 ＋ MM-01 相当のマルチモーダル/VLM 能力（無ければ先行実装）。ステージ4の伝ぴょんオンボードと連携。

## 仕様参照
docs/enhance/202607-demo-platform-plan.md §6（SBA-D）/ specs/13-multimodal.md

## 受け入れ条件（検証可能な述語で書く）
- [ ] 帳票アップロード→VLM-OCR読取→項目抽出・検証→登録ワークフローを実装
- [ ] サンプル帳票で OCR 抽出・検証が実環境で動く、を docs/verification/SBA-05.md に記録
- [ ] build / test / lint がパス

## 成果物
sample-app SBA-D 定義＋サンプル帳票 / OCRパイプライン / docs/verification/SBA-05.md

## 非ゴール / 制約
- VLM 能力（MM-01）が前提。未整備なら本タスク着手前に能力確認（人間ゲート）。
- 既存リソース・バケットは参照のみ。コミット/PR/push は人間承認後。
