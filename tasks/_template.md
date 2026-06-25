# タスク: <タイトル>

## ゴール
<一文で。何が達成されれば成功か>

## 対象 area
<web | api | both>  （loop-config.yml の areas を参照。test_cmd/lint_cmd の選択に使う）

## 受け入れ条件（検証可能な述語で書く）
- [ ] <例> packages/api の /api/auth テストが全件パスする
- [ ] <例> 既存の公開シグネチャ（API レスポンス契約）を壊さない
- [ ] <例> lint・型チェックがクリーン

## 非ゴール / 制約
- <例> DB / バケットのスキーマは変更しない
- 既存リソース（VCN develop / インスタンス dev / バケット jetuse-oci-source-documents）は参照のみ
- spec-driven: specs/ にない判断は実装せず docs/decisions/ に ADR 案を書く
