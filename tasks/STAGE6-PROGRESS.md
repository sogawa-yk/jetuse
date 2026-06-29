# ステージ6 進捗キュー（loop-runner / stage-runner の単一の真実源）

UI実装済み機能のバックエンド実体化（モック/未配線の解消）。2026-06-29 の OKE 実機デプロイ確認で判明した
「画面は動くがバックエンドが未実装/モック/未配線」の箇所を実体化し、**素のデプロイで全機能が実際に動く**状態にする。
`loop-runner` / `stage-runner` が依存順に消化する。status を更新するのは runner（人間がゲートを通した後 ／
stage-runner では Codex PASS＋自動統合後）。
詳細は各 `tasks/<id>.md`、索引は [`README-demo-platform-s6.md`](README-demo-platform-s6.md)、
親計画は [`../docs/enhance/202607-demo-platform-plan.md`](../docs/enhance/202607-demo-platform-plan.md) §9/§10 ステージ6。

status: `todo` | `in_progress` | `blocked` | `done`

前提: ステージ1–5 完了（S5=FE-01 done。S4=OKE 基盤 DEP-03 稼働。Platform broker PAPI / コネクタ CON / ASSET-01 はコア実装済）。
分類: **A**=未配線/501、**B**=render/plan-only、**C**=mock/fail-closed（実接続は人間ゲート）。**D**(設定で点灯)は本キュー対象外＝§10 enablement チェックリスト。

| 順 | タスク | 分類 | 依存 | 人間ゲート | status |
|---|---|---|---|---|---|
| 1 | BE-02 サンプルアプリのデータ自動マテリアライズ | B | S2(synth)・ENH-01(datasets) | apply/課金（実ADB・loop ADB再利用可） | **done** (wave1: PASS/8 E2E/統合済) |
| 2 | BE-01 デモ起動の実デプロイ配線（launch→OKE） | B | DEP-03(OKE) | **apply/課金**（OKE 実配備） | **done** (wave2: PASS/OKE dry-run E2E/統合済。実apply=人間ゲート) |
| 3 | BE-04 Platform RAG 検索の実体化 | A | PAPI-01..03 | apply/課金（GenAI/OS） | **blocked** (wave2: 実装済だが ADR-0018=テナント→ストア登録簿が spec外→adr_approval ゲートで Codex FAIL。未統合) |
| 4 | BE-05 スコープ承認 API＋UI（PAPI-02 到達経路） | A | PAPI-02 | — | **done** (wave1: PASS/3 E2E/統合済) |
| 5 | BE-07 スロット内RAG retrieval のベクトル化 | C(簡易→実) | S2(ai_runtime) | apply/課金（ベクトル索引） | **done** (wave1: PASS/E2E/統合済) |
| 6 | BE-03 コネクタ実行の実体化（Slack コア） | C | CON-02/03 | **Vault/IAM・実Slack資格情報** | **blocked** (wave3: invoke機構/Slackコア/Vault解決 実装＆mock E2E済[1195 pass]。残BLK=出荷デモのinvoke到達性=ADR-0019 R3 製品判断＋実Slack/Vault。未統合) |
| 7 | BE-06 ASSET-01 実接続（外部アプリSSO＋資産コネクタ） | C | ASSET-01・DEP-03 | **外部資産接続・SSO実設定(Identity Domain)** | **blocked** (wave3: SSO handoff/external-app store/asset MCP/marketplace install 実装＆mock E2E済。残BLK=実id_token nonce/identity検証=実IdP/Identity Domain変更＋ADR-0019承認。未統合) |
| 8 | BE-08 認証付きMCPサーバー登録 | A | — | **Vault書込IAM** | **done** (wave2: PASS/mock E2E/統合済。実Vault書込IAM=人間ゲート) |

## 実行可能集合（開始時）
- **自走可（jetuse-dev 内で実体化＋実機E2E）**: BE-02 / BE-04 / BE-05 / BE-07、および BE-01（実 apply は人間ゲートだが設計＋IaC＋検証まで自走）。
- **人間ゲート濃い（設計＋mock/loop-ADB E2E まで自走→実接続はステージ報告で一括提示）**: BE-03 / BE-06 / BE-08。
- 並行可: BE-02・BE-04・BE-05・BE-07 は相互独立（最大3で波運用）。BE-01 は DEP-03 のみ依存で独立。

## 人間ゲート（停止して承認を待つ）
- コミット / PR / push（全タスク共通）。
- **terraform apply・課金**: BE-01（OKE実配備）/ BE-04・BE-07（GenAI/ベクトル索引）/ BE-02（実ADB）。
- **Vault / IAM / 外部SaaS・SSO実設定**: BE-03（Slack）/ BE-06（伝ぴょん SSO・No.1資産）/ BE-08（Vault書込IAM）。
- **ADR**: 既存 ADR-0014/0015/0016/0017 を踏襲。BE-06 の SSO 実設計は ASSET 追補ADRを要する可能性（ドラフトは可・承認は人間ゲート）。

## ガバナンス（§4 の4制約を弱めない）
固定リファレンス基盤／制約付きパレット／合成バリデーション／越境防止＝Platform API ブローカー経由。
実体化しても**秘密は本体のみ保持・L3へ配らない**（ADR-0014 D5）／**デプロイ上限＝コンテナ**を維持。

## 監査の根拠（2026-06-29 / 3観点サブエージェント並列）
- フロント→endpoint マップ（packages/web 全 `/api` 呼出）。
- route 実装監査（service/routes/* と jetuse_core を REAL/STUB/DEGRADED 分類）。
- mock/stub/TODO/501/render-only/plan-only スキャン。
- 所見の詳細・file:line は 親計画 §10 ステージ6 の監査サマリ表、および memory [[jetuse-oke-deploy-feature-gaps]] を参照。

## 実行ログ（runner が追記）
- 2026-06-29 ステージ6 起票: OKE 実機デプロイ確認で UI実装済み・BE未実体の箇所を全面監査し、BE-01..08 と本キューを作成。
  施主指示により**起票のみ（ステージは開始しない）**。
