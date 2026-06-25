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
- エージェントが実行してよい操作: OCI CLI/SDKでのリソース参照、検証用リソースの作成・削除（**`jetuse-spike-` プレフィックス必須**）、Terraform plan。
- 人間の承認が必要な操作: 本番相当のTerraform apply、IAMポリシー変更、Identity Domain設定変更、スパイク用プレフィックス以外のリソース削除。
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
maker/checker を分離し、`/goal` の完了判定モデルが停止条件を採点する三層構成。

- **起動**: loop モードは `LOOP_TASK=<task> [GOAL="..."] [CODEX_MODEL=...] claude` で起動する。
  `LOOP_TASK` が無いセッションでは hooks は完全 no-op（通常開発に影響しない）。
  セッション内で `/goal <完了条件>` を実行してループを回す（条件は `loop-config.yml` の `goal_template`）。
- **毎ターン**: `loop-protocol` スキルの手順を厳守（実装→`codex-review`→履歴記録→STATE 更新）。
  レビュー判定（`review_verdict`）を自分で書き換えない。採点者は Codex。
- **単一の真実源**: 現在状態は `STATE.md`、不変の実行履歴は `runs/<run-id>/`（追記のみ）。設定は `loop-config.yml`。
- **自己改善**: 成果物の問題は `loop-doctor` スキルに渡す。コードでなく「ループの仕組み」を直す。
- **やってはいけないこと（人間ゲート）**: コミット / PR / push / リリースは承認なしに行わない。
  ループの仕組み（スキル・hooks・/goal 条件・設定）の編集は `loop-doctor` 経由・承認後のみ。
  段階引き上げ（report-only → auto-fix → auto-commit）も人間承認が必要。
- 詳細な使い方は `docs/loop-engineering.md`。
