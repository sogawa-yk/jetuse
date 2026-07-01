# JetUse on OCI — 生成AIユースケース基盤（Public版）

OCI Enterprise AI（OpenAI互換 agentic API）を基盤に、社内向け生成AIユースケースを束ねた
Webアプリのプロトタイプ。チャット / ユースケースエンジン / RAG / DBチャット(NL2SQL) /
エージェント（複数フレームワーク） / 音声（議事録・リアルタイム文字起こし・音声チャット） /
画像・映像分析を、OCIのマネージドサービス上で提供する。

> [English README](./README.en.md) ｜ アーキテクチャ図: [docs/architecture/system.md](./docs/architecture/system.md)
>
> 🚀 **新しく参加する開発者は [docs/guides/onboarding.md](./docs/guides/onboarding.md) から**（ローカル起動→テスト→自分専用E2E環境）
>
> 📚 **ドキュメント目次（ルーティング）: [docs/README.md](./docs/README.md)** ｜ 技術ナレッジまとめ: [docs/KNOWLEDGE.md](./docs/KNOWLEDGE.md)

## 🚀 ワンクリックデプロイ（Deploy to Oracle Cloud）

[![Deploy to Oracle Cloud](https://oci-resourcemanager-plugin.plugins.oci.oraclecloud.com/latest/deploy-to-oracle-cloud.svg)](https://cloud.oracle.com/resourcemanager/stacks/create?zipUrl=https://github.com/sogawa-yk/jetuse/archive/refs/heads/main.zip)

上のボタンから OCI Resource Manager のスタック作成に進み、**通常利用者は作業ディレクトリ `infra/orm`** を選んで Apply します。
VCN / Autonomous Database / API Gateway / Container Instance / Functions / Object Storage /
Identity Domain（OIDC）を一括構築し、**OIDC登録・DBセットアップまで含めて使えるアプリ**が立ち上がります。

- **管理者の初回作業**: 同じボタンから `infra/orm-bootstrap` を選び、対象コンパートメントの Dynamic Group / Policy とデプロイ担当グループ権限を一度だけ作成。
- **通常利用者の作業**: `infra/orm` を選択。テナンシ管理権限は不要で、Bootstrap で指定したグループへの所属だけが必要。
- **入力**: 対象コンパートメント、テナンシのホームリージョン、prefix。パスワード類は自動生成し、イメージは公開 OCIR を既定使用。
- **所要時間**: 初回は Autonomous Database 作成とDB初期化に約10〜15分。
- **デプロイ後**: 出力の `app_url` を開き、`demo_username` / `demo_password`（出力）でログイン。
- **前提**: Generative AI 対応リージョン、ADB・Identity Domain・Container Instance のクォータ、JetUse 専用コンパートメント。Bootstrap だけはテナンシ IAM 管理者が実行。
- 部門の管理者へ依頼する権限と Dynamic Group の一覧は [docs/setup/iam.md](./docs/setup/iam.md)、デプロイ手順は [docs/setup/orm.md](./docs/setup/orm.md)。

> ORM は Terraform のみを実行するため、コンテナイメージ（公開OCIR）・SPA配信・DB初期化・OIDC登録は
> それぞれ「事前公開イメージ」「Terraformオブジェクト配信」「コンテナ起動時の自己ブートストラップ」
> 「`oci_identity_domains_*` リソース」で自動化している（[docs/setup/orm.md](./docs/setup/orm.md)）。

## 機能

| 領域 | 機能 |
|---|---|
| チャット | ストリーミング会話、モデル選択、パラメータ/プリセット、短期メモリ、Markdown/Mermaid表示 |
| ユースケース | フォーム+プロンプトテンプレートの定義・共有（ビルダー）、組み込み5種 |
| RAG | 文書アップロード→引用付き回答（Vector Store / Select AI の2バックエンド） |
| DBチャット | 自然言語→SQL生成・実行（SQL Search / Select AI）、結果のグラフ化 |
| エージェント | ツール実行・MCP・記憶分離。エンジンは **native / OpenAI Agents SDK（既定） / LangGraph** を選択 |
| 音声 | 議事録（話者分離）、リアルタイム文字起こし、音声チャット（半二重） |
| マルチモーダル | 画像入力チャット、動画フレーム分析 |
| 管理・運用 | 監査ログ・利用ダッシュボード、入力モデレーション、レート制限、OCI Logging/Monitoring連携 |

## アーキテクチャ概要

- **フロント**: React SPA（Object Storage静的配信 + API Gateway、HashRouter）
- **API**: SSE系=Container Instance（FastAPI） / 非ストリーミング=OCI Functions（適材適所、ADR-0005）
- **AI**: OCI Enterprise AI（OpenAI互換 Responses/Chat Completions、IAM署名）。リージョン=大阪
- **データ**: ADB 26ai（会話・定義・議事録・SQL Search/Select AI）、Object Storage（文書・音声・ウォレット）
- **認証**: IAM Identity Domain（OIDC + PKCE）。SAMLフェデレーション手順あり
- 詳細とMermaid図 → [docs/architecture/system.md](./docs/architecture/system.md)

## リポジトリ構成

```
packages/web/    React SPA
packages/api/    FastAPI(service/) + Functionsルーター(fn/) + 共有ロジック(jetuse_core/)
infra/terraform/ Terraformモジュール群（environments/dev が実環境）
docs/            plan.md(計画) / decisions(ADR) / verification(検証レポート) /
                 comparison/(比較資料) / guides/(入門・運用) / setup(IAM・SAML手順) / tips.md(実機ハマり集)
specs/           機能仕様（フェーズごと）
```

## デプロイ（2系統）

### 前提
- OCIテナンシ（大阪リージョン推奨）、`~/.oci/config`、Terraform 1.15+ / Node 22 / Python 3.12 / podman
- 環境依存値は `.env`（雛形 `.env.example`）。**認証情報・OCID・エンドポイント実値はコミットしない**
- 人間の事前作業: IAM動的グループ/ポリシー（`docs/setup/iam.md`）、Identity Domain（`specs/06`）

### A. インフラ（Terraform）
```bash
cd infra/terraform/environments/dev
cp terraform.tfvars.example terraform.tfvars   # 値を設定（コミット禁止）
terraform init
terraform apply                                 # VCN/ADB/API GW/バケット/Functions/Logging 等
```
- ADBマイグレーション: `cd packages/api && python -m jetuse_core.migrate`（JETUSE_APPユーザー）

### B. アプリ
```bash
# API（SSE系・Container Instance）
cd packages/api
podman build -t <region>.ocir.io/<ns>/jetuse-dev-api:<ver> .
podman push  <region>.ocir.io/<ns>/jetuse-dev-api:<ver>
# → infra/.../terraform.tfvars の api_image_url を更新して terraform apply

# Functionsルーター（非ストリーミング）
podman build -f Containerfile.fn -t <region>.ocir.io/<ns>/jetuse-dev-fn-router:<ver> .
podman push <同上> ; tfvars の fn_router_image を更新して apply

# SPA（静的配信）
cd packages/web && npm install && npm run build && bash scripts/deploy.sh
```

> 運用の落とし穴（[docs/tips.md](./docs/tips.md) に詳説）:
> - 環境変数注入のためのCI再作成でも、**同じイメージタグだとコード変更は反映されない** → コード変更時は必ず再ビルド
> - OCIRへのpushはディスク逼迫時に静かに失敗しうる → apply前にレジストリ側でバージョン存在を確認
> - dev ADBは夜間停止に巻き込まれるため、作業前に起動確認（`ops/start-adb-if-stopped.sh`）

## 開発

> 新規参加者向けの手順は [docs/guides/onboarding.md](./docs/guides/onboarding.md)（初回セットアップ・自分専用E2E環境まで）。

```bash
# API（ローカル、認証オフ）
cd packages/api && AUTH_REQUIRED=false uvicorn service.main:app --port 8000
# SPA（/api を localhost:8000 へプロキシ）
cd packages/web && VITE_AUTH_REQUIRED=false npm run dev
```
- コミット前: `ruff check . && pytest`（API） / `npm run build && npm run lint`（web）
- 検証は実機確認主義（結果は `docs/verification/`）。複数人での実機E2Eは [docs/guides/dev-environments.md](./docs/guides/dev-environments.md)
- ブランチとリリース: `main` = Public 正式版、`dev` = Internal 次期版。Public の変更は `main` から `dev` へ forward merge（[運用詳細](./docs/guides/branching-and-releases.md)）。

## ドキュメント早見

| 知りたいこと | 参照 |
|---|---|
| 全体設計・図 | [docs/architecture/system.md](./docs/architecture/system.md) |
| 設計判断の理由 | [docs/decisions/](./docs/decisions/)（ADR） |
| AWS版参考実装との機能比較 | [docs/comparison/aws-reference.md](./docs/comparison/aws-reference.md) |
| 方式選定（RAG/NL2SQL/エージェントFW/コンピュート/アクセス制御） | `docs/comparison/` |
| カスタマイズ方法 | [docs/guides/customize.md](./docs/guides/customize.md) |
| デモ台本 | [docs/guides/demo-scenarios.md](./docs/guides/demo-scenarios.md) |
| 実機ハマり集 | [docs/tips.md](./docs/tips.md) |

## ライセンス / 位置づけ

`main` は Public 正式版、`dev` は Internal 正式版の次期開発ラインとして運用する。利用条件はリポジトリのライセンスを参照。
