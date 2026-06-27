# ステージ4 進捗キュー（loop-runner / stage-runner の単一の真実源）

コンテナデプロイ＋マーケット拡張＋既存資産オンボード（L3 配備・中央レジストリ μService・既存高機能資産の取込）。
`loop-runner` / `stage-runner` が依存順に消化する。status を更新するのは runner（人間がゲートを通した後 ／
stage-runner では Codex PASS＋自動統合後）。
詳細は各 `tasks/<id>.md`、索引は [`README-demo-platform-s4.md`](README-demo-platform-s4.md)、
親計画は [`../docs/enhance/202607-demo-platform-plan.md`](../docs/enhance/202607-demo-platform-plan.md) §3/§6/§7/§9/§10。

status: `todo` | `in_progress` | `blocked` | `done`

前提: ステージ3 完了（PAPI-01..03 / CON-01..03 done・PR #21 マージ済）。MM-01(VLM) 完了済（SBA-05/伝ぴょん multimodal 解錠）。

| 順 | タスク | 依存 | 人間ゲート | status |
|---|---|---|---|---|
| 1 | DEP-01 生成デモのコンテナ配備（L3） | ステージ3 | **ADR-0015 承認** / terraform apply・課金 | done（ADR-0015 採用・feat/stage-4 統合・terraform validate Success。実 apply は残ゲート） |
| 2 | MKT-01 sample-app/connector のマーケット流通 | ステージ1(PLG-04/05)・CON-01・SBA-01 | レジストリ apply・課金 | done（feat/stage-4 統合・mock E2E。実レジストリ apply は残ゲート） |
| 3 | DEP-02 Platform API 注入（D3 解） | DEP-01 | terraform apply・課金（実コンテナ）/ **ADR-0016 承認** | done（feat/stage-4 統合・mock E2E・ADR-0016 起票。実コンテナ apply は残ゲート） |
| 4 | MKT-02 中央レジストリ μService（署名・版・評価） | MKT-01 | μService apply・課金 | done（feat/stage-4 統合・migration 022・loop-ADB E2E。μService apply は残ゲート） |
| 5 | ASSET-01 既存資産オンボード（伝ぴょん/No.1-RAG/SQL-Assist） | DEP-01・CON-01・MKT-01 | **既存資産接続・SSO・apply（濃い）** | done（feat/stage-4b 統合・review-12 PASS・mock E2E。実資産接続/SSO は人間ゲート＝SKIPPED） |
| 6 | DEP-03 OKE 基盤への移行（JetUse 本体＋デモ） | ステージ4（DEP-01/02）・**ADR-0017** | **ADR-0017 承認 / OKE クラスタ apply・恒常課金** | done（feat/stage-4b 統合・review-26 PASS・ADR-0017採用・**OKE 実 apply 成功＋実機 deploy/inject/delete 検証済**） |

> 並行可: 起動直後は **DEP-01 と MKT-01 が相互独立で並行可（最大2）**。
> 続く波で **DEP-02（←DEP-01）と MKT-02（←MKT-01）が並行可**。ASSET-01 は DEP-01＋MKT-01 後。
> 単一セッションの loop-runner は「依存が満たされた todo の先頭」を1つずつ実行する。

> **⚠️ ステージ4 は apply/billing 依存が濃い**: L3 実コンテナ配備（DEP-01/02）・実レジストリ流通（MKT-01）・
> μService 実デプロイ（MKT-02）・既存資産接続（ASSET-01）はいずれも **terraform apply / 課金 / 既存資産接続**が
> 必要で、これらは stage-runner の **hard_gates（自走中は越えない）**。自走では「設計＋IaC plan＋コード＋
> mock/loop-ADB E2E＝PASS」まで進め、**実 apply を要する E2E は SKIPPED に明記してステージ報告で一括提示**する。

> **実行方式の選択**:
> - `stage-runner`（`.claude/loop/start-stage.sh stage-4`）: PASS タスクを `feat/stage-4` へ自動統合、
>   ステージ完了で1回だけ報告。apply/billing/ADR/IAM/既存資産接続は自走中も停止。

## 実行可能集合（開始時）
- DEP-01 と MKT-01（相互独立）。DEP-01 完了で {DEP-02, ASSET-01} 方向、MKT-01 完了で {MKT-02, ASSET-01} 方向が解禁。

## 人間ゲート（停止して承認を待つ）
- コミット / PR / push（全タスク共通）
- **ADR-0015 承認**: DEP-01（L3 ホスト型/既存資産オンボードの実行基盤・SSO・データ注入）
- **terraform apply・課金**: DEP-01（コンテナ配備）/ DEP-02（実コンテナ）/ MKT-01（実レジストリ）/ MKT-02（μService）
- **既存資産接続・SSO 実設定（濃い）**: ASSET-01（外部資産は参照のみ）

## ガバナンス（§4 の4制約を弱めない）
固定リファレンス基盤（既存 JetUse 基盤を再利用・新規アーキを作らない）／デプロイ上限＝コンテナ（L3）／
越境防止＝Platform API ブローカー経由でのデータ注入（DEP-02）／既存リソースは参照のみ。

## 起票予定 ADR
- **ADR-0015（DEP-01）**: L3 ホスト型/既存資産オンボード（実行基盤・SSO・データ注入）。計画 §11 で予約済。

## 実行ログ（runner が追記）
- 2026-06-27 ステージ4 起票: ステージ3 完了（PR #21 マージ）を確認し、202607-demo-platform-plan.md §10 ＋
  comparison/marketplace-plugin.md §2-B/§3 ＋ 既存基盤（ADR-0009/0011・container-instance・packages/registry・
  Platform API）を踏まえて DEP-01/02・MKT-01/02・ASSET-01 を `tasks/` へ落として本キューを作成。
  ADR-0015 は §11 で予約済（DEP-01 で起票）。MM-01(VLM) は完了済を確認。
- 2026-06-27 実行方式（施主判断）: 「ADR-0015 を先に確定」＋「ASSET-01 は後回し（外部資産専用パス）」。
  Wave 1 = DEP-01（spec+IaC plan+ADR-0015 ドラフト→ADR承認ゲートで停止）＋ MKT-01（mock E2E）→ MKT-02（loop-ADB E2E）
  を自走・統合。DEP-02 は DEP-01 依存で blocked、ASSET-01 は後回し。ADR-0015 承認＋必要なら apply 後に Wave 2。
- 2026-06-27 Wave 1 完了: MKT-01（review-4 PASS・mock E2E）→ MKT-02（review-4 PASS・loop-ADB E2E・migration 022）を統合。
  DEP-01 は review-21 PASS（21レビューの難物）で ADR-0015 ゲートに保持。
- 2026-06-27 ADR-0015 採用（施主承認・2点追記: §7 ライフサイクルは実apply前にDEP-02確定 / §8 ASSET-01 追補ADR）。
  DEP-01 を統合（terraform validate Success）→ Wave 2: DEP-02（review-3 PASS・mock E2E）を統合。
  **DEP-02 が ADR-0016（L3 デモ注入/ライフサイクル）を新規起票（提案中＝承認は人間ゲート）**。
  自走スコープ完了（DEP/MKT 4本 done・ASSET-01 deferred）。feat/stage-4 = api 902 passed / registry 108 / ruff clean / tf validate OK。
  残ゲート: ADR-0016 承認・terraform apply（複数）・base PR/push。ステージ報告: runs/_stages/stage-4/REPORT.md。
- 2026-06-27 stage-4b（S5 手前の残: OKE 移行＋既存資産 / 施主が apply・provisioning を確認不要で委譲）:
  feat/stage-4（PR #22 マージ済）を base に `feat/stage-4b` を切り、ASSET-01 と DEP-03 を自走・統合。
  ASSET-01（review-12 PASS・No.1-RAG/SQL-Assist=MCP・伝ぴょん=外部連携）、DEP-03（review-26 PASS・ADR-0017採用・
  配備層を K8s manifest/Secret 化）を統合。**OKE を実 apply（27 リソース・IAM ゲート無し）→ クラスタ
  jetuse-dev-oke-cluster(v1.35.2/2ノード) ACTIVE**。実機で namespace→Deployment(ConfigMap+Secret 注入)→Pod
  Running(env 確認)→namespace delete を実証。feat/stage-4b = api 990 passed / ruff clean / tf validate(OKE) OK。
  残ゲート: **base への PR/push（人間）**・JetUse 本体の実 cutover（イメージ build/push）・実 Slack・ASSET 実接続/SSO。
  ステージ報告: runs/_stages/stage-4b/REPORT.md。OKE tfstate は _stage-4b worktree 内（destroy 可・課金中）。
