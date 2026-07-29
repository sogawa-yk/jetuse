# STATE — PORT-03 公開スタックにホスト型エージェントを載せる

- run_id: `2026-07-29T1210_PORT-03`
- branch: `feat/PORT-03`（base = `main` / worktree 分離）
- goal: `runs/2026-07-29T1210_PORT-03/goal.txt`
- review_verdict: `FAIL`（review-6。**コード面の指摘はゼロ**。残る blocker は実機 E2E 未実施のみ）
- last_review_ref: `runs/2026-07-29T1210_PORT-03/reviews/review-6.json`
- updated_at: 2026-07-29

## 静的チェック

- `pytest packages/api/tests` 360 passed / `ruff check packages/api` clean
- `terraform validate`（orm / environments/dev）OK・`terraform fmt -check -recursive infra` OK
- `terraform test`: modules/iam 7 passed / modules/hosted-agent 2 passed（新設。CI にも追加済み）
- ローカル terraform は 1.6.6 で `mock_provider` 非対応。1.15.8 をスクラッチパッドへ入れて実行した
  （CI は `hashicorp/setup-terraform@v3` の最新なので影響なし）。

## 実装済み

- [x] T1 `infra/terraform/modules/hosted-agent/`（IDCS OAuth アプリ + 3SDK の Hosted Application/Deployment + カスケード削除ガード + tftest）
- [x] T2 `modules/iam` に `include_hosted_agent_principals`（**opt-in**）を追加し、有効時のみ runtime DG にホスト型2種を含める
- [x] T3 `infra/orm` 配線（`hosted_agents_enabled` / precondition 2本 / `time_sleep` によるIAM反映待ち / schema.yaml）
- [x] T4 `release.yml` に 3SDK イメージの build/push（kix・ord のみ）
- [x] T5 縮退の明確化（`capabilities.agents` + `hosted_agent.availability()` を判定の単一源に。UIへは理由文を返す）
- [x] T6 `agent_db.py` の base64 ウォレット対応 + テスト
- [x] T7 ドキュメント（`orm.md` / `iam.md` / `hosted-agent-oauth.md` / `cost-estimate.md` / `tips.md`）

## 実機 E2E の進捗（DEPLOYTEST / us-chicago-1）

| # | シナリオ | 結果 |
|---|---|---|
| 1 | 既存項目の非回帰 | ✅ **39/39 PASS・4xx/5xx 0件**（38項目 + 新設 `capability: agents`） |
| 2 | 3SDK のエージェント実行 | ✅ **9/9 PASS**。3SDK とも `get_current_time` のツール実行を伴う応答。「未設定」エラーなし |
| 3 | コールドスタート実測 | 🔄 ウォーム時 1.8〜2.3 秒を計測済み。`min_replica=0` からの実測はアイドル35分後に再計測中 |
| 4 | 無効化構成の縮退 | ⏸ |
| 5 | destroy | ⏸ |

証跡: `runs/2026-07-29T1210_PORT-03/e2e/`（OCID / ホスト名 / パスワードはマスク済み・漏れ無しを grep で確認）

## 実機で確定した事実（この run で判明・再検証不要）

- **provider 8.24.0 の `oci_generative_ai_hosted_application` は使えない**。work request は
  SUCCEEDED でリソースも ACTIVE になるのに、provider が `HOSTED_APPLICATION` と
  `hostedapplication` を照合して一致せず必ず失敗扱いにする。しかも tainted → 削除 → 再作成で
  **収束しない**。→ 作成・削除は `oci raw-request`、参照は data source に切り替えた。
- **`oci raw-request` は `--query` / `--output table` を無視する**。抽出は grep で行う。
- **`environment_variables.value` はスカラーだと引用符が残る**（provider が JSON 検証だけして
  そのまま送り、API も verbatim 保存する）。設定は `JETUSE_AGENT_CONFIG`（JSON 1本）で渡し、
  コンテナ側 `agent_env.py` が展開する。
- **画像タグを機能ごとに分けると壊れる**。エージェントだけ差し替えたら API が旧版のままで
  `capabilities.agents` が出なかった。`image_tag` 統一が必要（実害で裏付け）。
- `inbound_auth_config` の domain URL が実在しないと Application は CREATING → **FAILED**。
- ACTIVE な Hosted Deployment は直接削除できず、Application 削除でカスケードされる。
- **E2E ハーネスの穴を2件修正**: ログイン後の固定8秒待ち（トークン交換前に叩いて401）と
  `tone.wav` の生成漏れ（音声シナリオで異常終了）。

## 未完

- [ ] シナリオ3〜5（コールドスタート実測 / 縮退構成 / destroy）
- [ ] `docs/verification/PORT-03.md` の作成
- [ ] review-7 の指摘修正ぶんの再レビュー（証跡込み）

## 実機で確定した事実（この run で判明・再検証不要）

- **`environment_variables.value` はスカラーだと引用符が残る**。provider は JSON 文字列しか
  受け付けないのにアンマーシャルせず送り、API も verbatim 保存する（us-chicago-1 で実測）。
  → 設定は `JETUSE_AGENT_CONFIG` という **JSON オブジェクト1本**で渡し、
  コンテナ側 `agent_env.py` が `os.environ` へ展開する。sensitive map を `for_each` に
  使えない制約も同時に回避できる。
- `inbound_auth_config` の domain URL が実在しないと Application は CREATING → **FAILED**。
- ACTIVE な Hosted Deployment は直接削除できず、Application 削除でカスケードされる。
  Terraform の既定 destroy 順とは逆なので、`terraform_data.cascade_delete` で先に Application を消す。

## レビュー履歴（review-1 → review-6）

Codex 判定の推移: `blocker3/major4/minor1` → `blocker4/major6/minor1` → `blocker2/major4` →
`blocker1/major3` → **`blocker1/major0/minor0`**。残る1件は E2E 未実施（T8）。

主な指摘と対応（コード面はすべて解消済み）:

| 指摘 | 対応 |
|---|---|
| sensitive な map を `for_each` に使うと plan が停止する | 環境変数を JSON 1本（`JETUSE_AGENT_CONFIG`）に変更。実機で往復一致を確認 |
| 自給自足 OAuth に `allowed_scopes` が要る | 自分の fqs を登録（tips.md 2026-06-12 の実機記録どおり） |
| ACTIVE な Deployment は直接消せず destroy が失敗する | `terraform_data.cascade_delete` で Application を先行削除 |
| 公式要件の resource-type / 権限が不足 | `generativeaihostedapplicationiam` と `read repos` / `read vss-family` を追加（[公式](https://docs.oracle.com/en-us/iaas/Content/generative-ai/deploy-permissions.htm)で確認） |
| `client_secret` が Functions ルーターにも配られる | Container Instance だけへ渡すよう分離 |
| コンテナ側 `PROJECT_OCID` が空で RAG/LLM が失敗する | API が解決した値を invoke ステートで渡す。エージェント割当があればそれを優先（SPIKE-05） |
| project 解決失敗を握りつぶしている | 復旧手順つきの理由を SSE で返し、invoke しない |
| 未配備でも RAG 参照や project 自動作成の副作用が走る | dispatch 冒頭で配備状況を判定して即縮退 |
| 既存IAM流用時に黙って壊れる | plan 時 precondition + `existing_iam_covers_hosted_agents` |
| IAM 反映待ちが短い / 内容変更を検出しない | 600 秒 + IAM モジュールの `content_fingerprint` を trigger に |
| `latest` タグでは更新が反映されず、API とエージェントの契約がずれる | 全画像を共通 `image_tag` に束ね、配布ZIP生成時に commit SHA で固定 |
| iam モジュール変更が dev 等の既存呼び出し元にも及ぶ | `include_hosted_agent_principals` で opt-in 化（既定 false）+ tftest |

## 判断が要る事項（人間ゲート）

- **スコープ拡大**: `image_tag` の統一により、API / Functions ルーターの画像タグ運用も
  `latest` 固定から「配布ZIPは commit SHA 固定」へ変わる。PORT-03 の範囲を超えるが、
  エージェントだけ固定すると invoke ステートの契約が新旧で混在しうるため一体で変更した。
