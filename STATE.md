# STATE — PORT-03 公開スタックにホスト型エージェントを載せる

- run_id: `2026-07-29T1210_PORT-03`
- branch: `feat/PORT-03`（base = `main` / worktree 分離・**push / PR は未実施**）
- goal: `runs/2026-07-29T1210_PORT-03/goal.txt`
- review_verdict: `FAIL`（review-8。E2E 証跡は「十分」と判定され、残りはスクリプト堅牢性の指摘）
- last_review_ref: `runs/2026-07-29T1210_PORT-03/reviews/review-8.json`
- updated_at: 2026-07-29

## 実機 E2E（DEPLOYTEST / us-chicago-1）— 完了

| # | シナリオ | 結果 |
|---|---|---|
| 1 | 既存項目の非回帰 | ✅ **39/39 PASS・4xx/5xx 0件**（従来38 + 新設 `capability: agents`） |
| 2 | 3SDK のエージェント実行 | ✅ **9/9 PASS**（ツール実行を伴う応答・「未設定」エラー無し） |
| 3 | コールドスタート実測 | ✅ ウォーム 1.8〜2.3s / 35分アイドル後 **5.5〜6.4s** → 180秒 timeout で足りる |
| 4 | 無効化構成の縮退 | ✅ **6/6 PASS**（理由付き・内部識別子を出さない） |
| 5 | destroy | ✅ 成功・ホスト型リソース残存 0件 |

証跡: `runs/2026-07-29T1210_PORT-03/e2e/`（OCID / ホスト名 / パスワード / OS namespace /
ローカルパスをマスク済み）。レポート: `docs/verification/PORT-03.md`。

## 静的チェック

`pytest packages/api/tests` / `ruff check packages/api` / `terraform validate`（orm・dev・配布zip） /
`terraform fmt -check -recursive infra` / `terraform test`（iam・hosted-agent）がすべて緑。
ローカル terraform は 1.6.6 で `mock_provider` 非対応のため 1.15.8 を別途用意して実行した
（CI は `hashicorp/setup-terraform@v3` なので影響なし）。

## 実機で確定した事実（再検証不要）

- **provider 8.24.0 の `oci_generative_ai_hosted_application` は使えない**。work request は
  SUCCEEDED でリソースも ACTIVE なのに、provider が `HOSTED_APPLICATION` と
  `hostedapplication` を照合して一致せず失敗扱いにする。tainted → 削除 → 再作成で**収束しない**。
  → 作成・削除は `oci raw-request`、参照は data source に切り替えた。
- **`oci raw-request` は `--query` / `--output table` を無視する**。抽出は grep で行う。
- **`environment_variables.value` はスカラーだと引用符が残る**（provider が JSON 検証だけして
  そのまま送り、API も verbatim 保存）。設定は `JETUSE_AGENT_CONFIG`（JSON 1本）で渡す。
- **画像タグを機能ごとに分けると壊れる**。エージェントだけ差し替えたら API が旧版のままで
  `capabilities.agents` が出なかった → `image_tag` 統一。
- `inbound_auth_config` の domain URL が実在しないと Application は CREATING → **FAILED**。
- ACTIVE な Hosted Deployment は直接削除できず、Application 削除でカスケードされる。
- E2E ハーネスの穴2件（ログイン後の固定待ち / `tone.wav` 未生成）を修正済み。

## レビュー履歴

review-1 → review-8。判定の推移:
`b3/m4/mi1` → `b4/m6/mi1` → `b2/m4` → `b1/m3` → `b1/m0` → （E2E 実施）→ `b3/m4/mi2`。

review-8 は **E2E 証跡を "sufficient" と評価**したうえで、CLI スクリプトの堅牢性を指摘した。
対応済み:

| 指摘 | 対応 |
|---|---|
| API 失敗を「リソース無し」と取り違える（作成側） | CLI の終了コードをパイプの外で判定し、想定外の失敗は即終了 |
| 同（削除側）→ 実体を残して state だけ消える孤児化 | 一覧・所有権確認の失敗で destroy を失敗させ、state を保持する |
| 既存 Deployment を無検証で再利用 | 所有者タグ・artifact URI/tag・状態を検証し、不一致ならアプリごと作り直す |
| 既存IAM手順に3つ目の resource-type が抜けている | `generativeaihostedapplicationiam` を doc / precondition / schema に追加 |
| シェルの手動エスケープでは改行・Unicode を扱えない | リクエスト JSON を Terraform の `jsonencode` で組み立てる |
| 縮退ハーネスが中心2件を記録せず PASS しうる | 作成失敗時も必ず記録し、件数チェックを追加 |
| 意図的な無効化で `/api/health` 全体が赤くなる | `HOSTED_AGENTS_ENABLED` を渡し、未配備は `disabled`（集約対象外）と区別 |
| 証跡に OS namespace / ローカルパスが残る | マスクして再検査 |
| STATE.md が実態と乖離 | 本ファイルを更新 |

## 判断が要る事項（人間ゲート）

1. **方式変更の追認**: ADR-0019 は「Terraform で宣言的に組む」前提だったが、上流バグにより
   **OCI CLI 経由の作成 + data source 参照**へ変更した。ADR への追記が要る。
   provider が修正されたら通常の resource へ戻せる。
2. **再検証の要否**: 上表の堅牢性修正は E2E 成功**後**に入れたため、実機で再確認していない
   （単体テスト・plan・shell 構文チェックは緑）。マージ前にもう一度 apply → destroy を回すか、
   受け入れて後続で確認するかの判断。
3. **スコープ拡大**: `image_tag` 統一により API / Functions ルーターの配布も commit SHA 固定になる。
4. **push / PR**: 未実施。3コミット（実装 / 方式変更 / 検証）。
