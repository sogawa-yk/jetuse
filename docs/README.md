# ドキュメント目次（ルーティング）

OCI版 JetUse プロトタイプのドキュメント案内。**まずここから目的の資料へ辿れる**ことを目的とした索引。
（既存資料は一切削除していません。本ファイルと `KNOWLEDGE.md` は新規の案内・要約です。）

- 知見の横断まとめ → **[KNOWLEDGE.md](./KNOWLEDGE.md)**（ドメイン別の技術ナレッジ要約）
- 運用ルール・環境の確定事実 → リポジトリ直下 **[CLAUDE.md](../CLAUDE.md)**
- 作業計画の正本 → **[plan.md](./plan.md)**

---

## 1. はじめに／全体像

| 資料 | 内容 |
|---|---|
| [guides/onboarding.md](./guides/onboarding.md) | **新規開発者の入門** — ローカル起動→テスト→Gitフロー→自分専用E2E環境 |
| [guides/dev-environments.md](./guides/dev-environments.md) | 開発者ごとのデプロイ済みE2E環境(共有基盤+per-devアプリ層) |
| [guides/branching-and-releases.md](./guides/branching-and-releases.md) | Public (`main`) / Internal (`dev`) のブランチ・同期・正式リリース規約 |
| [../CLAUDE.md](../CLAUDE.md) | 運用ルール（spec-driven / 実機検証主義 / 比較ドキュメント主義）と環境の確定事実 |
| [plan.md](./plan.md) | 作業計画書（正本）。フェーズ・タスクチケット |
| [architecture/system.md](./architecture/system.md) | システムアーキテクチャ（Mermaid・正本） |
| [architecture/](./architecture/README.md) | OCI構成図（drawio/png）＋ユースケース別構成図（chat/agent/dbchat/minutes/ocr） |
| [guides/HANDOVER.md](./guides/HANDOVER.md) | 引き継ぎ資料（Phase 0完了時点の経緯） |
| [backlog.md](./backlog.md) | 既知の課題バックログ |
| [KNOWLEDGE.md](./KNOWLEDGE.md) | **本開発で得た知見のドメイン別まとめ（新規）** |
| [tips.md](./tips.md) | 発見・ハマり所の時系列メモ（実機確定の一次情報） |

## 2. 計画

| 資料 | 内容 |
|---|---|
| [plan.md](./plan.md) | 全体計画・タスクチケット書式（§16） |
| [plan-enhance.md](./plan-enhance.md) | 機能拡張(ENH-01〜10)の計画とゲート |
| [plan-gap-b.md](./plan-gap-b.md) | AWS版との差分「簡易版ギャップ(B項目)」解消計画 |

### プラットフォーム化構想（将来）

| 資料 | 内容 |
|---|---|
| [architecture/jetuse-product-concept.md](./architecture/jetuse-product-concept.md) | **JetUseプロダクトコンセプト（全体説明の正本）** — 実装済みOCI AIリファレンスを、顧客に刺さるWeb / SaaS UI・UXへ変換するReference-Guided Experience Builder |
| [architecture/experience-builder-implementation-strategy.md](./architecture/experience-builder-implementation-strategy.md) | **Experience Builder実装方針（正本）** — `main`安定版を維持し、実RAG、高品質Redwood UI、Slack Hybrid Experienceから実証して段階拡張 |
| [architecture/ai-application-builder-vision.md](./architecture/ai-application-builder-vision.md) | AIアプリケーションビルダー初期構想（最新方針へのリンクを追記） |
| [enhance/202607.md](./enhance/202607.md) | エンハンス案原文（プラグイン機構・マーケットプレイス・エージェント開発基盤化） |
| [comparison/marketplace-plugin.md](./comparison/marketplace-plugin.md) | 上記の実現方式比較（プラグイン4ティア・隔離モデル・レジストリ・既存資産取込） |

## 3. 意思決定（ADR）

| ADR | 決定 |
|---|---|
| [ADR-0001](./decisions/ADR-0001-spike-environment.md) | スパイク環境（コンパートメント・モデル方針） |
| [ADR-0002](./decisions/ADR-0002-conversation-state.md) | 会話状態の正はADB |
| [ADR-0003](./decisions/ADR-0003-sse-path.md) | SSEはAPI Gateway経由 |
| [ADR-0004](./decisions/ADR-0004-frontend-static-hosting.md) | フロントはAPI GW + Object Storage静的配信 |
| [ADR-0005](./decisions/ADR-0005-functions-vs-container.md) | Functions優先・SSEのみContainer Instances |
| [ADR-0006](./decisions/ADR-0006-long-term-memory.md) | 長期メモリはOCIネイティブ(LTM + memory_subject_id) |
| [ADR-0007](./decisions/ADR-0007-agents-sdk-chat-completions.md) | Agents SDKはChatCompletionsModel経由 |
| [ADR-0008](./decisions/ADR-0008-agents-sdk-default-engine.md) | エージェント標準エンジン=OpenAI Agents SDK |
| [ADR-0009](./decisions/ADR-0009-hosted-react-three-sdk.md) | エージェントを3SDK別Hosted Applicationに集約 |
| [ADR-0014](./decisions/ADR-0014-public-distribution-and-release-lines.md) | Public配布のIAM分離とPublic/Internalリリースライン |

## 4. 比較ドキュメント（プリセールス転用可・定量比較付き）

| 資料 | テーマ |
|---|---|
| [comparison/aws-reference.md](./comparison/aws-reference.md) | AWS版参考実装 機能比較（定点観測） |
| [comparison/rag-backends.md](./comparison/rag-backends.md) | RAG 4方式（Vector Store/File Search・Select AI RAG・Agents KB・OpenSearch） |
| [comparison/nl2sql-backends.md](./comparison/nl2sql-backends.md) | NL2SQL（SQL Search vs Select AI NL2SQL） |
| [comparison/agent-frameworks.md](./comparison/agent-frameworks.md) | エージェント実装方式 |
| [comparison/agent-runtimes.md](./comparison/agent-runtimes.md) | エージェント実行ランタイム（hosted SDK 3種 vs Select AI Agent） |
| [comparison/translation.md](./comparison/translation.md) | 翻訳（OCI Language vs Enterprise AI LLM, SPIKE-E5） |
| [comparison/realtime-transport.md](./comparison/realtime-transport.md) | リアルタイムSTTの転送方式 |
| [comparison/compute-architecture.md](./comparison/compute-architecture.md) | APIコンピュート構成（ARCH-01） |
| [comparison/access-control.md](./comparison/access-control.md) | アクセス制御（IP制限・レート制限） |
| [comparison/marketplace-plugin.md](./comparison/marketplace-plugin.md) | プラグイン機構／マーケットプレイス（プラットフォーム化、§2に詳細） |
| [guides/ocr-limits-and-workarounds.md](./guides/ocr-limits-and-workarounds.md) | OCR(Document Understanding)の制限と回避（ENH-07） |

## 5. セットアップ（人間作業が必要なもの）

| 資料 | 内容 |
|---|---|
| [setup/iam.md](./setup/iam.md) | **Public版 IAM Bootstrap** — 管理者/通常利用者の分離とruntime権限一覧 |
| [setup/public-iam-requirements.md](./setup/public-iam-requirements.md) | **Public版 IAM要件（提出用）** — 役割別Policy、Dynamic Group、管理者依頼テンプレート |
| [setup/dynamic-group-matching-rules.md](./setup/dynamic-group-matching-rules.md) | **Dynamic Group compact構成** — dev/public共有とdeploy-test専用のMatching Rule |
| [setup/public-deploy-dedicated-compartment.md](./setup/public-deploy-dedicated-compartment.md) | **専用コンパートメント利用者向け** — テナンシ権限なしでDeploy to OCIを実行する手順 |
| [setup/public-deploy-tenancy-admin.md](./setup/public-deploy-tenancy-admin.md) | **テナンシ管理者向け** — Dynamic Group / Policyの準備からデプロイ・可動確認まで |
| [setup/orm.md](./setup/orm.md) | Deploy to Oracle Cloud の二段階デプロイ（管理者Bootstrap→通常利用者） |
| [setup/hosted-agent-oauth.md](./setup/hosted-agent-oauth.md) | ホスト型エージェントのOAuth/IDCS設定 |
| [setup/saml-federation.md](./setup/saml-federation.md) | SAMLフェデレーション手順 |

## 6. 検証レポート（実機検証主義の一次成果）— テーマ別

### 6.1 基盤・インフラ・デプロイ
[SPIKE-01](./verification/SPIKE-01.md)(Responses基礎) ／ [SPIKE-02](./verification/SPIKE-02.md)(GW越しSSE) ／
[APP-01](./verification/APP-01.md)(FastAPI) ／ [APP-02](./verification/APP-02.md)(React SPA) ／
[INFRA-01](./verification/INFRA-01.md)(Terraform) ／ [INFRA-02](./verification/INFRA-02.md)・[02b](./verification/INFRA-02b.md)・[02c](./verification/INFRA-02c.md)(Identity Domain/OIDC) ／
[ARCH-01](./verification/ARCH-01.md)(コンピュート構成) ／ [ARCH-02-04](./verification/ARCH-02-04.md)(Functions移行) ／
[OPS-01](./verification/OPS-01.md)(管理画面) ／ [OPS-02](./verification/OPS-02.md)(可観測性) ／
[perf-refactor](./verification/perf-refactor.md)(リファクタ後性能) ／ [refactor-validation-report](./verification/refactor-validation-report.md)(リファクタ検証総括)

**リファクタリング・レビュー**: [refactoring/](./refactoring/README.md)（[review-validation](./refactoring/review-validation.md)＝検証済み版 ／ [review-validation-audit](./refactoring/review-validation-audit.md)＝監査）

### 6.2 チャット
[CHAT-01](./verification/CHAT-01.md)(ストリーミング) ／ [CHAT-02](./verification/CHAT-02.md)(会話永続化) ／
[CHAT-03b](./verification/CHAT-03b.md)(コード/Mermaid) ／ [CHAT-03c](./verification/CHAT-03c.md)(UX改善) ／
[CHAT-04-06](./verification/CHAT-04-06.md)・[CHAT-04b](./verification/CHAT-04b.md)(パラメータ/検索/記憶) ／
[CHAT-07-09](./verification/CHAT-07-09.md)(タイムアウト/キャンセル/削除同期) ／
[SPIKE-05](./verification/SPIKE-05.md)(Conversations/記憶) ／ [SPIKE-10](./verification/SPIKE-10.md)(長期メモリAPI探索) ／
[CP2-measurements](./verification/CP2-measurements.md)(TTFT/トークン計測)

### 6.3 RAG・検索・DB
[SPIKE-03](./verification/SPIKE-03.md)(Vector Store/File Search) ／ [RAG-01-02](./verification/RAG-01-02.md)(ファイル管理+引用) ／
[SPIKE-08](./verification/SPIKE-08.md)・[RAG-03](./verification/RAG-03.md)(Select AI RAG) ／
[SPIKE-E2](./verification/SPIKE-E2.md)(OpenSearch RAG) ／
[SPIKE-04](./verification/SPIKE-04.md)・[SQL-01](./verification/SQL-01.md)・[SQL-02](./verification/SQL-02.md)・[SQL-03-04](./verification/SQL-03-04.md)(NL2SQL/SQL Search) ／
[ENH-01](./verification/ENH-01.md)(CSV取込) ／ [ENH-02](./verification/ENH-02.md)(テーブルプレビュー) ／
[SPIKE-E3](./verification/SPIKE-E3.md)(Trusted Answer Search 調査)

### 6.4 エージェント
[SPIKE-09](./verification/SPIKE-09.md)(Responsesツール機構) ／ [AGT-01](./verification/AGT-01.md)・[AGT-01c](./verification/AGT-01c.md)(Function Calling/ツール) ／
[AGT-02](./verification/AGT-02.md)(MCP) ／ [AGT-03](./verification/AGT-03.md)(Agent Builder) ／
[AGT-04](./verification/agt-04.md)・[GAP-04](./verification/GAP-04.md)・[SPIKE-G4](./verification/SPIKE-G4.md)(ホスト型エージェント) ／
[AGT-05](./verification/AGT-05.md)(長期メモリ統合) ／ [AGT-MULTI](./verification/AGT-MULTI.md)(3SDK別Hosted ReAct) ／
[FW-01](./verification/FW-01.md)(Agents SDK) ／ [FW-02](./verification/FW-02.md)(LangGraph) ／ [FW-03-04](./verification/FW-03-04.md)(CrewAI/LangChain比較) ／
[SPIKE-ADK](./verification/SPIKE-ADK.md)(Google ADK) ／ [SPIKE-E1](./verification/SPIKE-E1.md)・[ENH-04](./verification/ENH-04.md)(Select AI Agent)

### 6.5 音声・映像・翻訳
[SPIKE-06](./verification/SPIKE-06.md)(OCI Speech) ／ [VOICE-01](./verification/VOICE-01.md)(議事録) ／
[VOICE-02](./verification/VOICE-02.md)(リアルタイムSTT) ／ [VOICE-03](./verification/VOICE-03.md)(音声チャットv1) ／
[MM-01](./verification/MM-01.md)(画像入力/映像分析) ／ [ENH-09](./verification/ENH-09.md)(映像分析修正) ／
[ENH-10](./verification/ENH-10.md)(リアルタイム翻訳) ／ [SPIKE-G5](./verification/SPIKE-G5.md)(全二重化の可否)

### 6.6 OCR / ドキュメント理解
[SPIKE-E4](./verification/SPIKE-E4.md)(Document Understanding 可用性/日本語精度) ／
[guides/ocr-limits-and-workarounds.md](./guides/ocr-limits-and-workarounds.md)(制限・回避・VLMエンジン)

### 6.7 UI
[SPIKE-07](./verification/SPIKE-07.md)(Redwood風試作) ／ [UI-01-03](./verification/UI-01-03.md)(実装・自己検証) ／
[UC-01-03](./verification/UC-01-03.md)(ユースケースエンジン)

### 6.8 セキュリティ・ガバナンス
[SEC-02](./verification/SEC-02.md)(入力モデレーション/監査) ／ [SEC-03](./verification/SEC-03.md)(IP/レート制限) ／
[GAP-01](./verification/GAP-01.md)・[SPIKE-G1](./verification/SPIKE-G1.md)(ガードレール) ／
[GAP-02](./verification/GAP-02.md)・[SPIKE-G2](./verification/SPIKE-G2.md)(SAML) ／ [SPIKE-G3](./verification/SPIKE-G3.md)(Code Interpreter相当)

### 6.9 配布・ドキュメント
[DOC-01-04](./verification/DOC-01-04.md)(配布・ドキュメント) ／ [guides/customize.md](./guides/customize.md)(カスタマイズ) ／
[guides/demo-scenarios.md](./guides/demo-scenarios.md)(デモシナリオ)

## 7. フィードバック・UI素材

- feedbacks/ — ユーザーフィードバック原文（リポジトリには含めずローカル管理）
  - ※プラットフォーム化のエンハンス案は §2 の [enhance/202607.md](./enhance/202607.md)
- [ui/](./ui/) — UIモック・スクリーン例（[plan](./ui/plan.md) ／ [tokens-report](./ui/tokens-report.md) ／背景テクスチャ）
- [checkpoints/CP2.md](./checkpoints/CP2.md) — チェックポイント②

---

## SPIKE 採番の対応（早見表）

- **SPIKE-01〜10**: Phase 0 基礎検証（Responses/SSE/Vector Store/SQL/記憶/Speech/UI/Select AI RAG/ツール機構/長期メモリ）
- **SPIKE-G1〜G5**: AWS版との差分ギャップ(GAP)の実現可能性（モデレーション/SAML/Code Interpreter/AgentCore/全二重音声）
- **SPIKE-E1〜E5**: 機能拡張(enhance)の調査ゲート（E1=Select AI Agent, E2=OpenSearch, E3=Trusted Answer Search, E4=OCR, E5=翻訳※comparison/translation.md）
- **SPIKE-ADK**: Google ADK実証
