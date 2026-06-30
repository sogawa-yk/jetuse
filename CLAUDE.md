# OCI版 JetUse プロトタイプ — 運用ルール

作業計画の正本: `docs/plan.md`。本ファイルは運用ルールと環境の確定事実の要約。

## 開発方式

- **spec-driven**: 各タスクは `specs/` 配下の仕様を正とする。仕様にない実装判断が必要になったら、実装せず `docs/decisions/` にADR案を書いて人間レビューを要求する。
- **1タスク = 1ブランチ**。完了時にmainへマージ。GitHubリモート接続後はPR運用（動作確認ログ添付必須）に移行。
- **実機検証主義**: 「ドキュメントにそう書いてある」は完了条件にならない。OCI実環境での実行結果をもって完了とする。検証結果は `docs/verification/` にレポートとして残す。
- **比較ドキュメント主義**（ユーザー指示 2026-06-11）: 複数のOCIサービス/方式の選択肢から1つを採用する場合は、`docs/comparison/` に比較ドキュメントを残す（プリセールス転用可能な粒度。可能なら定量比較付き）。実機の発見・Tipsは `docs/tips.md` に追記。
- **コミット前チェック**: lint / type check / unit test を通す。フロントは `npm run build` 成功まで。

## 環境・認証の扱い

- OCI認証は `~/.oci/config`（DEFAULTプロファイル）。**認証情報・テナンシ/コンパートメントOCID・エンドポイント実値をリポジトリにコミットしない**。環境依存値は `.env`（gitignore済み）に置き、雛形は `.env.example`。
- エージェントが実行してよい操作: OCI CLI/SDKでのリソース参照、Terraform plan、**`jetuse-dev` コンパートメント内の開発用実リソースの作成・変更・削除（ユーザー承認済み 2026-06-25）**。
  - **jetuse-dev 内のリソース作成は必ず Terraform（IaC）で行う**（ad-hoc な CLI/SDK 作成ではなく `infra/terraform/` または ORM スタックに書いて `terraform apply`）。プレフィックスは不要（`jetuse-spike-` 制約は jetuse-dev には適用しない）。
  - **むやみにリソースを増やさない**: 同種の用途には既存リソースを再利用する。仕様変更で作り直す場合は Terraform で破棄→再作成し、不要リソースを残さない。
- 人間の承認が必要な操作: IAMポリシー変更、Identity Domain設定変更（テナンシレベル変更。`enable_iam=false` 維持）、本番相当のTerraform apply、`jetuse-dev` 以外のコンパートメントへの apply。
- 既存リソース（VCN `develop`、インスタンス `dev`、バケット `jetuse-oci-source-documents`）は参照のみ。削除・変更禁止。

## 環境の確定事実（2026-06-10時点）

- 実行環境: OCI computeインスタンス `dev`（VM.Standard.E6.Flex / Oracle Linux 9.7 / ap-osaka-1。ブートボリューム150GB — 2026-06-13拡張・ユーザー承認）。
- コンパートメント: `jetuse-proto`（OCIDは `.env` の `COMPARTMENT_OCID`）。計画書の `jetuse-spike` は存在しないため代替使用（ADR-0001）。
- ツール: Python 3.12（venv: `.venv`）/ Node 22 / Terraform 1.15 / podman 5.6 / OCI CLI 3.85。
- **大阪リージョン（ap-osaka-1）はOpenAI互換 agentic API フル対応**: ベースURL `https://inference.generativeai.ap-osaka-1.oci.oraclecloud.com/openai/v1` 配下に Responses / Conversations / Files / Vector Stores / File Search / Code Interpreter。
- 認証は IAM署名（`oci-genai-auth` パッケージでopenai-pythonに署名注入）を採用。
- 大阪のオンデマンドモデル: gpt-oss-120b/20b, command-a-03-2025, command-a-reasoning/vision, gemini-2.5-pro/flash, llama-3.3-70b 等。**Grok系・Llama 4系は大阪不可**（ADR-0001）。
- OCI Speech: STT（バッチ/リアルタイム）はWhisperモデルで日本語対応。**TTSはPhoenix限定**。
- API GatewayのSSE対応は文書未保証（readTimeout最大300秒）→ SPIKE-02で実測。

## リポジトリ構成

```
CLAUDE.md            # 本ファイル
docs/plan.md         # 作業計画書（正本）
specs/               # 機能仕様（フェーズごと）
docs/decisions/      # ADR
docs/verification/   # スパイク・検証レポート
spikes/              # Phase 0 検証スクリプト
packages/web/        # React SPA
packages/api/        # FastAPI
infra/terraform/     # Terraformモジュール
infra/orm/           # Resource Managerスタック
```

## タスクチケット書式

`docs/plan.md` §16 を参照。

## ループエンジニアリング（loop-impl.md / ADR-0012）

実装は Claude Code（maker）、レビューは Codex（checker）。別ツール・別モデルで
maker/checker を分離する。完了条件は `GOAL` env（→ `runs/<id>/goal.txt` に記録）と
プロンプトで与え、**エージェント自身が `loop-protocol` を毎ターン辿って自走**することでループが回る。
（注: `/goal` というスラッシュコマンドは未実装。Stop hook `log_turn.sh` はターン記録のみ。）

- **起動**: 推奨は worktree 分離起動 `[GOAL="..."] [CODEX_MODEL=...] .claude/loop/start-loop.sh <task>`。
  タスクごとに独立した git worktree（既定 `../<repo名>-loops/<task>`）を作り、その中で
  `LOOP_TASK=<task> claude` を起動する。**複数 loop の並行運用でもブランチ/作業ツリーを共有せず衝突しない**。
  後始末は `.claude/loop/end-loop.sh <task>`。後方互換の共有チェックアウト起動
  `LOOP_TASK=<task> [GOAL="..."] claude` も可だが**並行起動は禁止**（衝突する）。
  `LOOP_TASK` が無いセッションでは hooks は完全 no-op（通常開発に影響しない）。
  完了条件は起動時の `GOAL="..."` で登録する（`loop-config.yml` の `goal_template` を当該タスクで具体化）。
  オーケストレータが無人ペインで並列起動する場合は `LOOP_AUTONOMOUS=1` を付け、権限プロンプトで
  止まらず自走させる（コミット/PR/push/apply 等のハードゲートは `--disallowedTools` で遮断＝飛ばさない）。
- **毎ターン**: `loop-protocol` スキルの手順を厳守（実装→`codex-review`→履歴記録→STATE 更新）。
  レビュー判定（`review_verdict`）を自分で書き換えない。採点者は Codex。
- **単一の真実源**: 現在状態は `STATE.md`、不変の実行履歴は `runs/<run-id>/`（追記のみ）。設定は `loop-config.yml`。
- **ステージ承認ループ（`stage-runner` / loop-runner の上位）**: 人間ゲートを「タスク単位」から
  「**ステージ単位**」へ引き上げる方式。`.claude/loop/start-stage.sh <stage>` で起動し、`STAGE<N>-PROGRESS.md`
  のキューを波で自走させ、**PASS したタスクを ステージ専用ローカルブランチ `feat/stage-<N>` へ自動 commit+merge**
  して波を繋ぎ、キュー枯渇まで進めて**ステージ完了で1回だけ人間へ報告**（`runs/_stages/<stage>/REPORT.md`）。
  自動統合は**この隔離ブランチ限定**で、**push / base への PR / apply / 課金 / IAM / ADR 承認は自走中も停止**する
  （`loop-config.yml` の `stage_runner.hard_gates`）。自動コミットはオーケストレータのみ（タスクエージェントの
  権限 deny は据え置き＝多層防御）。波間の統合衝突はサブエージェント解決→`codex-review`→緑なら継続/不能なら停止。
  導入は人間承認済み（2026-06-26 / loop-doctor）。
- **自己改善**: 成果物の問題は `loop-doctor` スキルに渡す。コードでなく「ループの仕組み」を直す。
- **やってはいけないこと（人間ゲート）**: コミット / PR / push / リリースは承認なしに行わない。
  ループの仕組み（スキル・hooks・完了条件(GOAL/goal_template)・設定）の編集は `loop-doctor` 経由・承認後のみ。
  段階引き上げ（report-only → auto-fix → auto-commit）も人間承認が必要。
- 詳細な使い方は `docs/loop-engineering.md`。
