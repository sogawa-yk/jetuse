# OCI版 JetUse プロトタイプ — 運用ルール

作業計画の正本: `docs/plan.md`。本ファイルは運用ルールと環境の確定事実の要約。

## 開発方式

- **spec-driven**: 各タスクは `specs/` 配下の仕様を正とする。仕様にない実装判断が必要になったら、実装せず `docs/decisions/` にADR案を書いて人間レビューを要求する。
- **1タスク = 1ブランチ + PR**。**4ブランチ体制**（ADR-0028）: `main`（Public 安定・Deploy ボタンの配信元）/ `public-dev`（Public 統合）/ `internal-dev`（Internal 統合）/ `internal-stable`（Internal 安定）。正本は `docs/guides/branching-and-releases.md`。
  - **共有変更は `public-dev` 起点**で `public-dev` へ入れ、`public-dev → internal-dev` で同期する。Internal 固有変更のみ `internal-dev` 起点。**`internal-dev` を `public-dev` / `main` へ merge しない**（merge は先端を丸ごと運ぶため内部固有が漏れる。後から公開するなら cherry-pick）。
  - **`main` / `internal-stable` へ feature branch を直接向けない。** release PR と hotfix のみ。`main` への merge は即座に公開配信物（`orm-main` の ZIP と `:latest` イメージ）を差し替える。
  - **どちら起点か判定の目安**: 共有物（docs・CLAUDE.md・specs・`.claude/` ループ機構・公開アプリコード・infra・ops 等）なら **`public-dev` 起点**。**`ops/internal-only-paths.txt` に列挙されたもの**のみ `internal-dev` 起点。共有物を internal 側にすると `main` に届かず両系統が乖離する（実例: 2026-07 の docs 整理を dev 起点にして main 側 PR を後追いで足す羽目になった）。**この判定は `ops/check-branch-base.sh` が CI（`pull_request`）で検査する**。ローカルは base を推測できないため既定でスキップ＝**合格ではない**。手元で確かめるなら `BRANCH_BASE=internal-dev make lint`。
  - **同期は `ops/sync-public-to-internal.sh`** を使う（同期ブランチを `refactor/*` で切り deploy-dev.yml の自動配備を回避。push / PR は人間ゲート）。
  - **DB migration の番号帯**: Public=`0xx_` / Internal 固有=`5xx_`。既存の重複（`017`〜`021`）はリネームしない（`schema_migrations` の記録と食い違うため）。
- **実機検証主義**: 「ドキュメントにそう書いてある」は完了条件にならない。OCI実環境での実行結果をもって完了とする。検証結果は `docs/verification/` にレポートとして残す。
- **比較ドキュメント主義**（ユーザー指示 2026-06-11）: 複数のOCIサービス/方式の選択肢から1つを採用する場合は、`docs/comparison/` に比較ドキュメントを残す（プリセールス転用可能な粒度。可能なら定量比較付き）。実機の発見・Tipsは `docs/tips.md` に追記。
- **コミット前チェック**: `make lint && make test && make build` を通す（単一コマンド入口は root `Makefile`。`make help` で一覧。build/test/lint/e2e/deploy/down）。
- **破壊的スクリプトは明示フラグ必須**（ヘッドレス安全）: `ops/dev-env-up.sh <dev> --apply`（terraform apply）／`ops/dev-env-down.sh <dev> --yes`（destroy）／`ops/recreate-agents.sh <tag> --yes`（削除）。無フラグは plan のみ／拒否で停止する（対話プロンプトは持たない）。
- **ローカル開発フロー**: 実装 → `make test`（単体） → `make deploy DEV=<名>`（自分の app スタック） → 実 OCI へ E2E（`tasks/<id>.md` のシナリオ・**dev コンパートメント限定・モック不可**） → コミット。各段が単一コマンド。事前に必要な OCI リソース＝共有 `environments/dev` apply 済・自分の ADB スキーマ（`ops/setup-dev-schema.py`）・`infra/terraform/environments/app/<dev>.tfvars`・OCIR ログイン（詳細 `docs/guides/dev-environments.md`）。
- **報告・委譲（個人スキル・ホーム側 `~/.claude` が自動読込）**: 人が読む成果物は **`preview` スキル**で Obsidian の `_renders/<topic>.html`（単一ファイル・`.obsidian-dir` で出力先解決）へ出す（リポジトリ内ドキュメントは markdown のまま・HTML はコミットしない）。**ループの報告書（タスクパケット / ステージ報告）も同じ経路**に乗せる＝リポジトリは様式・中身・証跡だけを持ち、置き場の解決は各自のホーム側スキルに委ねる（契約 `docs/guides/report-pipe.md`・設定 `loop-config.yml` の `report:`・ADR-0018）。長時間タスクは **`dispatch-remote` スキル**でインスタンス `dev` へ無人委譲（結果は `agent/<id>` ブランチで返る）。

## 環境・認証の扱い

- **OCI ログイン方式は1スイッチ（`jetuse_core.oci_auth` リゾルバ・env `AUTH_MODE`。分岐を各所に散らさず必ず経由する）**:
  - `config_file`（既定）= `~/.oci/config` のプロファイル（`OCI_PROFILE` で選択・空=DEFAULT）。**ローカル(macOS)開発の既定**。
  - `resource_principal` = 配備済みサービス（Container Instance / Functions）。Terraform が `AUTH_MODE=resource_principal` を注入。
  - `instance_principal` = OCI インスタンス自身。**インスタンスへの無人委譲（dispatch-remote）実行時に `AUTH_MODE=instance_principal`**（`dev` インスタンスで有効確認済）。
- **認証情報・テナンシ/コンパートメント OCID・エンドポイント実値をリポジトリにコミットしない**。環境依存値は `.env`（gitignore 済み）に置き、雛形は `.env.example`。
- エージェントが実行してよい操作: OCI CLI/SDKでのリソース参照、検証用リソースの作成・削除（**`jetuse-spike-` プレフィックス必須**）、Terraform plan。
- 人間の承認が必要な操作: 本番相当のTerraform apply、IAMポリシー変更、Identity Domain設定変更、スパイク用プレフィックス以外のリソース削除。
- 既存リソース（VCN `develop`、インスタンス `dev`、バケット `jetuse-oci-source-documents`）は参照のみ。削除・変更禁止。

## 環境の確定事実（2026-06-10時点）

- **開発モデル（2026-07-26〜）**: **ローカル(macOS)が主開発環境**（実装・単体は `make`。OCI認証は config_file＝`~/.oci/config`）。**OCI compute インスタンス `dev`（VM.Standard.E6.Flex / OL9.7 / ap-osaka-1・ブートボリューム150GB）は長時間タスクの無人実行機**（`dispatch-remote` で `claude -p` を委譲・`AUTH_MODE=instance_principal`）。移動でローカルが切れても委譲実行は継続する。コードは OneDrive 配下に置かない（`.git` 破損回避）。
- コンパートメント: `jetuse-proto`（OCIDは `.env` の `COMPARTMENT_OCID`）。計画書の `jetuse-spike` は存在しないため代替使用（ADR-0001）。
- ツール: Python 3.13（ローカル macOS・venv: `.venv`。2026-07-28 に 3.12→3.13 へ更新。インスタンス `dev` は 3.12 のまま）/ Node 22 / Terraform 1.15（2026-08-03 に 1.6.6→1.15.8 へ更新。**1.7 未満だと `terraform test` の `mock_provider` が動かず `make lint` が落ちる**）/ **コンテナエンジンは podman か docker のどちらか**（`ops/_container.sh` が解決。`JETUSE_CONTAINER_ENGINE` で明示指定可。2026-08-04 時点のローカルは docker 29 で podman 未導入） / OCI CLI 3.85。
- **大阪リージョン（ap-osaka-1）はOpenAI互換 agentic API フル対応**: ベースURL `https://inference.generativeai.ap-osaka-1.oci.oraclecloud.com/openai/v1` 配下に Responses / Conversations / Files / Vector Stores / File Search / Code Interpreter。
- 認証は IAM署名（`oci-genai-auth` パッケージでopenai-pythonに署名注入）を採用。
- 大阪のオンデマンドモデル: gpt-oss-120b/20b, command-a-03-2025, command-a-reasoning/vision, gemini-2.5-pro/flash, llama-3.3-70b 等。**Grok系・Llama 4系は大阪不可**（ADR-0001）。
- OCI Speech: STT（バッチ/リアルタイム）はWhisperモデルで日本語対応。TTSは**Phoenix限定ではない**（2026-07-28実測: us-chicago-1 可 / 大阪・トロント不可）。`TTS_REGION` 未指定ならデプロイリージョン → us-phoenix-1 の順に試行。
- API GatewayのSSE対応は文書未保証（readTimeout最大300秒）→ SPIKE-02で実測。

## リポジトリ構成

```
CLAUDE.md      # 本ファイル（プロジェクト規約）。個人共通規約はホーム側 ~/.claude が自動読込
Makefile       # 単一コマンド入口（make build/test/lint/e2e/deploy/down・make help）
docs/          # plan.md(正本) / decisions=ADR / verification / comparison / guides / tips.md / archive(退避)
specs/         # 機能仕様（フェーズごと）
packages/web/  # React SPA（Vite/Tailwind・build→Object Storage）
packages/api/  # FastAPI(service/) + Functions(fn/) + 共有ロジック(jetuse_core/・認証は jetuse_core/oci_auth.py)
packages/*     # jetuse_shared(セキュリティlib) / registry(登録簿) / agent-containers / hosted-agent-sample
infra/terraform/  # Terraform（modules/ + environments/{dev=共有基盤, app=開発者ごと}）
infra/orm/     # ワンクリック Resource Manager スタック（IAM+アプリ）
ops/           # 運用スクリプト（dev-env-up/down・deploy・sync-public-to-internal・check-branch-base 等。破壊系は明示フラグ必須）
.claude/       # ループ機構（skills/hooks/loop・下記「ループエンジニアリング」）
```

## タスクチケット書式

`docs/plan.md` §16 を参照。

## ループエンジニアリング（loop-config.yml / docs/loop-engineering.md）

実装は Claude Code（maker）、レビューは Codex（checker）。別ツール・別モデルで maker/checker を分離し、
エージェントが毎ターン `loop-protocol` を辿って自走することでループが回る（採点者は Codex。判定を Claude が書き換えない）。

- **仕組みの所在**: スキル `.claude/skills/{loop-protocol,loop-runner,stage-runner,codex-review,loop-doctor}`、
  hooks `.claude/hooks/`、起動スクリプト `.claude/loop/`、設定 `loop-config.yml`。詳細は `docs/loop-engineering.md`。
- **起動**: worktree 分離起動 `[GOAL="..."] .claude/loop/start-loop.sh <task>`（後始末 `end-loop.sh`）。
  `LOOP_TASK` が無いセッションでは hooks は完全 no-op（通常開発に影響しない）。
- **毎ターン**: `loop-protocol` の手順（実装→`codex-review`→履歴記録→STATE 更新）を厳守。
  完了ゲート = review_verdict=PASS かつ area の test/lint 緑 かつ実環境 E2E 通過。PASS 後は非 blocker を追わず停止（手順5.5）。
- **単一の真実源**: 現在状態は `STATE.md`（**ローカル・git 追跡外**。worktree ごとに持ち、コミットしない）、不変の実行履歴は `runs/<run-id>/`（追記のみ）。完了時に `runs/<run-id>/STATE.md` へ写しを残すので、後から状態を辿れる。
- **自己改善**: 成果物の問題は `loop-doctor` へ（コードでなく「ループの仕組み」を直す）。
- **人間ゲート**: コミット / PR / push / リリース、および仕組み（スキル・hooks・完了条件・設定）の編集は承認なしに行わない。
