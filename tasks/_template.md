# タスク: <タイトル>

## ゴール
<一文で。何が達成されれば成功か>

## 対象 area
<web | api | both>  （loop-config.yml の areas を参照。test_cmd/lint_cmd の選択に使う）

## 受け入れ条件（検証可能な述語で書く）
- [ ] <例> packages/api の /api/auth テストが全件パスする
- [ ] <例> 既存の公開シグネチャ（API レスポンス契約）を壊さない
- [ ] <例> lint・型チェックがクリーン

## E2E シナリオ（実環境 / jetuse-dev・複数）
完了ゲートで Claude が jetuse-dev の固定 loop 環境へデプロイ（loop-config.yml の area `deploy_cmd`）し、
下記シナリオを実行して証跡を `runs/<run-id>/e2e/` に残す。最低 2 本（e2e.min_scenarios）。
Codex は実行せず、この証跡＋diff を評価する。実施不能な範囲は `runs/<run-id>/e2e/SKIPPED.md` に理由明記。
- [ ] <シナリオ1: 期待結果と確認方法（HTTP応答 / DB状態 / スクショ等の証跡）>
- [ ] <シナリオ2: 異常系・境界系など別観点>
- [ ] <該当なしの範囲があれば: 理由を SKIPPED.md に書く対象を列挙>

## 非ゴール / 制約
- <例> DB / バケットのスキーマは変更しない
- 既存リソース（VCN develop / インスタンス dev / バケット jetuse-oci-source-documents）は参照のみ
- spec-driven: specs/ にない判断は実装せず docs/decisions/ に ADR 案を書く
