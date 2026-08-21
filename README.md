# JetUse on OCI — 生成AIユースケース基盤（Public版）

OCI Enterprise AI（OpenAI互換 agentic API）を基盤に、チャット / ユースケース / RAG / DBチャット(NL2SQL) /
エージェント / 音声 / 画像・映像分析を1つのWebアプリにまとめたプロトタイプ。すべてOCIのマネージドサービス上で動く。

[English README](./README.en.md)

## デプロイ

[![Deploy JetUse to Oracle Cloud](https://oci-resourcemanager-plugin.plugins.oci.oraclecloud.com/latest/deploy-to-oracle-cloud.svg)](https://cloud.oracle.com/resourcemanager/stacks/create?zipUrl=https://github.com/sogawa-yk/jetuse/releases/download/orm-main/jetuse-orm.zip)

このボタンは、IAMとアプリ本体を含む1つのTerraformスタックをOCI Resource Managerへ渡す（Working directoryの指定は不要）。
VCN / Autonomous Database / API Gateway / Container Instance / Functions / Object Storage / Identity Domain を
一括構築し、出力の `app_url` に `demo_username` / `demo_password` でログインできる状態になる。初回は10〜15分。

- 入力は対象コンパートメントと `prefix` 程度。パスワードは自動生成、コンテナイメージは公開OCIRを使う。
- 実行ユーザーのIAM権限に応じて `enable_dynamic_group` / `enable_runtime_policy` を切り替える。
- 対応リージョン・サービス枠・事前チェックリストは [Resource Managerガイド](./docs/setup/orm.md)。
  必要な権限は [Public版 IAM要件](./docs/setup/public-iam-requirements.md) と [IAMガイド](./docs/setup/iam.md)。

## 機能

| 領域 | 機能 |
|---|---|
| チャット | ストリーミング会話、モデル選択、パラメータ/プリセット、短期メモリ、Markdown/Mermaid表示 |
| ユースケース | フォーム+プロンプトテンプレートの定義・共有（ビルダー）、組み込み5種 |
| RAG | 文書アップロード→引用付き回答（Vector Store / Select AI の2バックエンド） |
| DBチャット | 自然言語→SQL生成・実行（SQL Search / Select AI）、結果のグラフ化 |
| エージェント | ツール実行・MCP・記憶分離。エンジンは native / OpenAI Agents SDK（既定） / LangGraph |
| 音声 | 議事録（話者分離）、リアルタイム文字起こし、音声チャット（半二重） |
| マルチモーダル | 画像入力チャット、動画フレーム分析 |
| 管理・運用 | 監査ログ・利用ダッシュボード、入力モデレーション、レート制限、OCI Logging/Monitoring連携 |

## アーキテクチャ

- **フロント**: React SPA（Object Storage静的配信 + API Gateway、HashRouter）
- **API**: SSE系=Container Instance（FastAPI） / 非ストリーミング=OCI Functions（ADR-0005）
- **AI**: OCI Enterprise AI（OpenAI互換 Responses/Chat Completions、IAM署名）
- **データ**: ADB 26ai（会話・定義・議事録・NL2SQL）、Object Storage（文書・音声・ウォレット）
- **認証**: IAM Identity Domain（OIDC + PKCE）。SAMLフェデレーション手順あり

詳細とMermaid図 → [docs/architecture/system.md](./docs/architecture/system.md)

## 開発

初回セットアップから自分専用のE2E環境までは [オンボーディングガイド](./docs/guides/onboarding.md)。

```bash
cd packages/api && AUTH_REQUIRED=false uvicorn service.main:app --port 8000  # API（認証オフ）
cd packages/web && VITE_AUTH_REQUIRED=false npm run dev                     # SPA（/api を :8000 へ）

make lint && make test && make build   # コミット前チェック（入口は root Makefile・一覧は make help）
make deploy DEV=<名>                    # 自分専用のOCI環境へ配備してE2E
```

```
packages/web/    React SPA
packages/api/    FastAPI(service/) + Functionsルーター(fn/) + 共有ロジック(jetuse_core/)
infra/           terraform/(モジュールと環境) + orm/(ワンクリックスタック)
docs/            設計・ADR・検証レポート・運用ガイド
specs/           機能仕様（フェーズごと）
```

ブランチは `main`（Public安定版・Deployボタンの配信元）/ `public-dev`（Public統合）/
`internal-dev` / `internal-stable`。Publicの変更は `public-dev` へ入れ、リリース時に `main` へ運ぶ
（[ブランチとリリース](./docs/guides/branching-and-releases.md)）。検証は実機確認主義（結果は `docs/verification/`）。

## ドキュメント

目次は [docs/README.md](./docs/README.md)。よく見るもの:

| 知りたいこと | 参照 |
|---|---|
| 全体設計・図 | [docs/architecture/system.md](./docs/architecture/system.md) |
| 設計判断の理由 | [docs/decisions/](./docs/decisions/)（ADR） |
| 方式選定（RAG/NL2SQL/エージェントFW/コンピュート） | [docs/comparison/](./docs/comparison/) |
| カスタマイズ方法 | [docs/guides/customize.md](./docs/guides/customize.md) |
| デモ台本 | [docs/guides/demo-scenarios.md](./docs/guides/demo-scenarios.md) |
| 実機ハマり集 | [docs/tips.md](./docs/tips.md) |
