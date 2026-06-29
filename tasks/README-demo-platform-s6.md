# ステージ6 索引 — UI実装済み機能のバックエンド実体化

親計画: [`../docs/enhance/202607-demo-platform-plan.md`](../docs/enhance/202607-demo-platform-plan.md) §9/§10 ステージ6。
進捗キュー（正本）: [`STAGE6-PROGRESS.md`](STAGE6-PROGRESS.md)。
背景メモ: [[jetuse-oke-deploy-feature-gaps]]（OKE 実デプロイで判明した素デプロイの機能ギャップ）。

## 目的
2026-06-29 の OKE 実機デプロイ確認で「画面は動くがバックエンドが未実装/モック/未配線」と判明した箇所を実体化し、
**素のデプロイで全機能が実際に動く**プロダクト状態にする。分類 A=未配線/501・B=render/plan-only・C=mock/fail-closed を対象
（D=設定で点灯 は §10 enablement チェックリストで別扱い）。

## タスク
| ID | 概要 | 分類 | 主因(file) |
|---|---|---|---|
| [BE-01](BE-01.md) | デモ起動の実デプロイ配線（launch→OKE 実配備） | B | `hearing.py` launch / `deploy.py`・`deploy_inject.py` 未配線 |
| [BE-02](BE-02.md) | サンプルアプリのデータ自動マテリアライズ | B | `synth.py` SeedPlan=plan-only |
| [BE-03](BE-03.md) | コネクタ実行の実体化（Slack コア） | C | `connector_runtime.py` http_caller 拒否 / `platform.py` invoke 501 |
| [BE-04](BE-04.md) | Platform RAG 検索の実体化 | A | `platform.py` rag/search 501 |
| [BE-05](BE-05.md) | スコープ承認 API＋UI | A | `platform_grants.approve_scopes` route 無し |
| [BE-06](BE-06.md) | ASSET-01 実接続（外部アプリSSO＋資産コネクタ） | C | `external_app.py` / `asset_connectors.py` shape のみ |
| [BE-07](BE-07.md) | スロット内RAG retrieval のベクトル化 | C | `ai_runtime.py` 語彙重なりスコア |
| [BE-08](BE-08.md) | 認証付きMCPサーバー登録 | A | `agents.py` 501（Vault書込IAM） |

## 実行
`stage-runner`（`.claude/loop/start-stage.sh stage-6`）。A/B（BE-01/02/04/05/07）は jetuse-dev 内で自走・実機E2E可。
C で外部SaaS/SSO/Vault-IAM を要する BE-03/06/08 は設計＋mock/loop-ADB E2E まで進め、実接続は人間ゲートとして報告で一括提示。
**※ 施主指示により現時点では起票のみ。開始しない。**
