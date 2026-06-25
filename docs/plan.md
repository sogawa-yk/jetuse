# OCI版 JetUse — コーディングエージェント作業計画書

最終更新: 2026-06-10
対象: Claude Code（または同等のコーディングエージェント）に実行させるタスク計画。
人間（施主）の役割は、OCI環境の認証情報準備・コンソール側の有効化作業・各チェックポイントでのレビューに限定する。

---

## 0. 運用ルール（CLAUDE.mdに転記する内容）

### 開発方式
- **spec-driven**: 各タスクは `specs/` 配下の仕様を正とする。仕様にない実装判断が必要になったら、実装せず `docs/decisions/` にADR案を書いて人間レビューを要求する。
- **1タスク = 1ブランチ = 1PR**。PRには動作確認ログ（コマンド実行結果）を必ず添付。
- **実機検証主義**: 「ドキュメントにそう書いてある」は完了条件にならない。OCI実環境での実行結果をもって完了とする。スパイク・実装とも、検証結果は `docs/verification/` にレポートとして残す。
- **コミット前チェック**: lint / type check / unit test を通す。フロントは `npm run build` 成功まで。

### 環境・認証の扱い
- OCI認証は実行マシンの `~/.oci/config`（人間が事前設定）を使用。**認証情報・テナンシOCID・エンドポイントURLをリポジトリにコミットしない**。環境依存値はすべて `.env.example` + `terraform.tfvars.example` に雛形化。
- エージェントが実行してよい操作: OCI CLI/SDKでのリソース参照、検証用リソースの作成・削除（`jetuse-spike-*` プレフィックス必須）、Terraform plan。
- **人間の承認が必要な操作**: Terraform apply（課金リソース作成）、IAMポリシー変更、Identity Domain設定変更、リソース削除（スパイク用プレフィックス以外）。

### リポジトリ構成（最初のタスクで作成）
```
jetuse-oci/
├── CLAUDE.md                  # 本ルール + アーキテクチャ要約
├── specs/                     # 機能仕様（フェーズごと）
│   ├── 00-architecture.md
│   ├── 01-design-system.md    # Redwood風UI仕様
│   ├── 02-chat.md  ...
├── docs/
│   ├── decisions/             # ADR
│   ├── verification/          # スパイク・検証レポート
│   └── comparison/aws-reference.md     # AWS版参考実装との機能比較表（定点観測）
├── packages/
│   ├── web/                   # React SPA
│   └── api/                   # FastAPI
├── infra/
│   ├── terraform/             # モジュール群
│   └── orm/                   # Resource Managerスタック定義
└── .github/workflows/
```

---

## 1. デザイン方針（specs/01-design-system.md の骨子）

### 結論: React + Tailwind + Redwood風テーマ（Oracle JETは不採用）
- 理由: コーディングエージェントの部品生産性、JetUse型SPA（動的フォーム生成・SSEストリーミング・Markdown/Mermaidレンダリング）との親和性。JET採用はADRで不採用理由を明文化（UI-SPIKEで最終確認）。
- **Redwood風デザイントークン**を `theme.ts` + Tailwind configに定義:
  - プライマリ: Oracle Red系（#C74634近傍）、アクセント控えめ。背景はRedwood特有の暖色ニュートラル（クリーム〜グレージュ）+ ダークモード
  - 角丸・余白・エレベーションはRedwoodの「フラット+細罫線」基調。AWSコンソール風の青系・密度高いUIにしない
  - フォント: OSS配布を考慮しOracle Sansは使わない。近い印象のOSSフォント（例: Inter / Noto Sans JP）を採用し、`theme` で差し替え可能に
- **リブランド可能性**: ロゴ・プライマリカラー・プロダクト名を単一の `branding.json` で差し替え可能にする（顧客カスタマイズ要件）。デフォルトがOracleっぽい、というだけの状態にする
- レイアウトはJetUse踏襲: 左ナビ（ユースケース一覧、履歴）/ メイン（フォーム+チャット）/ 右パネル（設定）

---

## 2. Phase 0: 実機スパイク（エージェントタスクとして実行）

各スパイクは独立タスク。成果物は `docs/verification/SPIKE-XX.md`（目的 / 手順 / 実行ログ / 結果 / 設計への影響 / 残課題）。
リージョンは大阪を第一候補とし、機能が無い場合のみ他リージョン（シカゴ等）で検証し差分を記録。

**人間の事前作業（スパイク開始前に1回）**:
- 検証用コンパートメント `jetuse-spike` 作成、エージェント用ユーザー/グループへのポリシー付与（GenAI, ADB, Object Storage, Speech, API Gateway, Container Instances の manage）
- OCI GenAIのモデルアクセス確認（リージョンの提供モデル一覧をコンソールで確認）
- `~/.oci/config` 設定

### SPIKE-01: Responses API 基礎検証
- 内容: openai-pythonクライアントのbase_url差し替えでOCI Responses APIに接続。①非ストリーミング応答 ②SSEストリーミング ③モデル列挙（Grok 4.1 Fast / Command A / Llama / gpt-oss等、大阪で使えるもの）④usage取得
- 完了条件: 各モデルでストリーミング応答が動くサンプルスクリプト + レイテンシ/TTFT計測表
- 設計判断への入力: モデル切替UIに載せるデフォルトモデル一覧

### SPIKE-02: ストリーミング経路検証（API Gateway越しSSE）
- 内容: 最小FastAPI（SSE中継）をContainer Instancesにデプロイし、OCI API Gateway経由でブラウザからSSEを受信。①バッファリングの有無 ②タイムアウト上限 ③切断時の挙動
- 完了条件: 60秒以上の連続ストリーミングがAPI GW経由で成立するか判定。不成立の場合はLB直結構成の代替検証まで実施し、ADRで経路を決定
- 注意: API GWデプロイ等のapplyは人間承認後にエージェントが実行

### SPIKE-03: Vector Store / File Search 検証
- 内容: 日本語ドキュメント（社内規程風のダミー3種: PDF/docx/md）をVector Storeに取り込み、File Searchツール経由でRAG応答。①対応形式・サイズ上限 ②日本語チャンク品質（検索ヒットの妥当性を10問で採点）③メタデータフィルタ ④引用情報の取得形式
- 完了条件: 10問の評価表 + 引用表示に使えるレスポンス構造のドキュメント化

### SPIKE-04: SQL Search (NL2SQL) 検証
- 内容: ADB（スパイク用にAlways FreeまたはECPU最小）にSHサンプルスキーマを展開し、SQL Searchのセマンティック濃縮を登録 → 日本語の自然言語質問10問でSQL生成品質を評価。生成SQLのみでアプリ側実行が前提であることをAPIレベルで確認
- 完了条件: 質問→生成SQL→人手検証の評価表、セマンティック濃縮のセットアップ手順書（Terraform/スクリプト化の見通し付き）
- 比較: 同じ10問をADB内のSelect AI（runsql）でも実行し、品質・運用性を比較記録

### SPIKE-05: Conversations / Projects / 記憶検証
- 内容: Responses APIのConversations（会話状態）、Projects（記憶分離）、長期記憶・短期記憶圧縮の挙動確認。アプリ側でADBに履歴を持つ設計とどう棲み分けるか（二重管理問題）を検証
- 完了条件: 「履歴の正はADB、Responses API側状態は実行時コンテキスト」とする設計の妥当性判定ADR案

### SPIKE-06: OCI Speech 検証
- 内容: ①バッチ文字起こし（日本語会議音声、話者分離）②リアルタイムSTTのWebSocket接続 ③TTSの日本語品質。音声チャットの半二重パイプライン（録音→STT→LLM→TTS）の最小プロトタイプ
- 完了条件: 各機能の品質メモ + 音声チャットv1のUX制約リスト（議事録機能を先行させるかの判断材料）

### SPIKE-07 (UI-SPIKE): Redwood風デザインシステム試作
- 内容: Tailwind + デザイントークンで主要コンポーネント（ナビ、チャットバブル、フォーム部品、テーブル、トースト）のギャラリーページを作成。Redwoodの公式デモ/OCIコンソールのスクリーンショットと並べて「Oracleっぽさ」を人間がレビュー
- 完了条件: 人間レビューでルック&フィール承認 → `specs/01-design-system.md` 確定。branding.json差し替えのデモ込み
- 並行検証: JetUseフロント（MIT-0）のコンポーネント実装を読み、流用可能な部品（Markdownレンダラ、ストリーミング表示、フォーム生成ロジック）を特定してリスト化。**見た目は流用しない、ロジックは選択的に流用**の線引きを明確化

### Phase 0 出口判定（人間チェックポイント①）
- 全スパイクレポートを集約し、`specs/00-architecture.md` を確定版に更新
- スコープ修正（例: Vector Store日本語品質が低ければSelect AI RAGを主バックエンドへ昇格、SSE不成立ならLB直結へ変更）をこの時点で反映

---

## 3. Phase 1: 基盤構築

### INFRA-01: Terraformモジュール群
- VCN / ADB / Object Storage / API Gateway / Container Instances / OCIRの各モジュール。`environments/dev` で全体合成。plan までエージェント、apply は人間承認制
- 完了条件: dev環境一式がplanクリーン → apply成功（人間承認後）→ destroy/再applyの冪等性確認

### INFRA-02: IAM Identity Domain（OIDC）
- OIDC機密アプリ登録のTerraform化（可能な範囲）。不可能な手作業はスクリーンショット付き手順書 `docs/setup/idcs.md` に切り出し
- 完了条件: SPAからのPKCEログイン→JWT取得→API GW or FastAPIでの検証が通る

### INFRA-03: ORMスタック化
- `infra/orm/` にスタック定義 + schema.yaml（入力変数のUI化）。Deploy to Oracle Cloudボタン用のREADMEセクション
- 完了条件: 別コンパートメントへのボタンデプロイで全リソースが立つこと（人間が1回実演）

### APP-01: FastAPIスケルトン
- JWT検証ミドルウェア、設定管理（pydantic-settings + feature flags）、構造化ログ、ヘルスチェック、OpenAPIドキュメント
- 完了条件: 認証付きhello worldがAPI GW経由で応答、テスト雛形あり

### APP-02: React SPAスケルトン
- Vite + React + Tailwind（SPIKE-07のデザインシステム適用）、OIDCログインフロー、レイアウトシェル（左ナビ/メイン）、ダークモード、i18n雛形（日英）
- 完了条件: ログイン→空のチャット画面表示までE2Eで動作

### CI-01: GitHub Actions
- lint/test/build、コンテナビルド→OCIR push→Container Instances更新（dev自動、本番は手動トリガ）
- 完了条件: mainマージで dev に自動反映

---

## 4. Phase 2: コアチャット

- CHAT-01: Responses API統合サービス層（モデル抽象、ストリーミング、リトライ、usage記録）
- CHAT-02: 会話永続化（ADBスキーマ作成、CONVERSATIONS/MESSAGES、SODAまたはJSONリレーショナル。マイグレーション機構含む）
- CHAT-03: チャットUI（SSE表示、中断、再生成、コードブロック/Markdown/Mermaidレンダリング、コピー）
- CHAT-04: モデル切替・パラメータ設定UI、システムプロンプトのプリセット管理
- CHAT-05: 履歴一覧・検索・タイトル自動生成・削除・共有リンク
- CHAT-06: **【必須・ユーザー指示2026-06-10】Enterprise AI Agentsの短期メモリ統合** — Conversations APIと履歴圧縮(compaction)を採用し長会話のトークン消費・レイテンシを削減。ADBの会話レコードにconversation idを紐付け（履歴の正はADBのまま — ADR-0002）。retention設定とstoreの扱いを設計に含める（現状は意図しない蓄積防止のためstore=false運用）
- 完了条件（人間チェックポイント②）: JetUseの「チャット」と同等操作感のデモ。TTFT・体感速度を記録

## 5. Phase 3: ユースケースエンジン + 標準ユースケース

- UC-01: プロンプトテンプレートエンジン（変数定義 → 動的フォームUI自動生成）。**これが本プロジェクトの心臓部**。input_schema(JSON) → フォームのレンダラを単体テスト厚めに
- UC-02: 標準ユースケース移植: 要約 / 執筆・校閲 / 翻訳 / Webコンテンツ抽出（バックエンドfetch+本文抽出） / ダイアグラム生成。各ユースケースはUC-01エンジン上のテンプレート定義として実装（コードでなくデータで増やせることを証明）
- UC-03: ユースケースビルダーUI（作成→プレビュー→保存→タグ・公開共有）
- 完了条件: 非開発者がビルダーで新ユースケースを5分で作れること（人間が実演テスト）

## 6. Phase 4: RAG

- RAG-01: ファイル管理（アップロード→Object Storage→Vector Store登録、非同期ステータス、削除同期）
- RAG-02: RAGチャット（File Search、引用元表示UI）
- RAG-03: Select AI RAGバックエンド（DBMS_CLOUD_AIプロファイル/ベクトルインデックスの初期化SQL自動化、バケット自動更新）。configでバックエンド切替
- RAG-04: `docs/comparison/rag-backends.md`（presales転用可能な比較ドキュメント。SPIKE-03/04の評価手法を再利用した定量比較付き）

## 7. Phase 5: DBチャット（NL2SQL）— 差別化の山場

- SQL-01: セマンティック濃縮セットアップの自動化（デモスキーマ + 日本語ビジネス用語辞書）
- SQL-02: NL2SQLチャットフロー（質問→生成SQL提示→ユーザー確認→読み取り専用ユーザーで実行→結果テーブル）。SELECT以外拒否・行数上限・タイムアウトのガード実装
- SQL-03: 結果の自動グラフ化（JetUseのChart Tool相当。チャート種別をLLMに提案させる）
- SQL-04: Select AI直接実行モード（オプション）と使い分けdocs
- 完了条件（人間チェックポイント③）: 顧客デモ可能品質。日本語10問の正答率を定点指標化

## 8. Phase 6: エージェント

- AGT-01: Function Callingフレームワーク（ツールレジストリ、Web検索ツール、Code Interpreter built-in tool統合、ツール実行の承認UI）
- AGT-02: MCPチャット（MCPサーバー登録UI、認証情報はOCI Vault保存、MCP Calling経由実行）
- AGT-03: Agent Builder（エージェントCRUD、Project割当による記憶分離、タグ・公開共有）
- AGT-04: Applications/Deploymentsインポート（LangGraph製サンプルエージェント1体のデプロイ手順込み。Galley既存エージェントを題材化）— **2026-06-12 完了**: IAM整備後にデプロイ〜invoke E2E成功（未文書のinvoke URL規則を実機確定）。アプリへの本格統合はPhase 9の実行基盤として実施（docs/verification/agt-04.md）
- AGT-05: **【必須・ユーザー指示2026-06-10】Enterprise AI Agentsの長期メモリ統合** — `subject_id`（=JWTのsub）による会話横断のパーソナライズ。Project単位の記憶分離（AGT-03）と整合させ、ガバナンス（保持期間・削除権）をPhase 10要件に接続

## 9. Phase 7: UI改善 — OCIコンソール風管理画面（2026-06-12追加・ユーザー指示）

作業指示の正本: **docs/ui/plan.md**（レイアウトの正=docs/ui/screen-example-*.png、トークンの正=Redwood Design System。矛盾時はスクリーンショット優先）。

- UI-01: Redwoodトークン抽出（oj-redwood.cssからライト/ダーク変数を抽出スクリプトで解決 → src/styles/tokens.css + tokens-report.md。**レポート提示後に人間確認** — neutral-170抽出が確認ポイント）
- UI-02: 実装（システムフォントスタック、ハードコード色禁止=tokens.css参照、コンポーネント分解を先に提示）
- UI-03: Playwright自己検証（スクリーンショット比較→修正、最大2周）
- 禁止: oj-redwood.css全文読込 / redwood.oracle.comへのfetch / 本物のOCIコンソール操作
- **Playwright MCPサーバーを利用するため専用セッションで実施**（Phase 6完了時にHANDOVER.mdで引き継ぎ）

## 10. Phase 8: 音声・マルチモーダル

- VOICE-01: 議事録生成（バッチ文字起こし+話者分離→議事録/FAQ/記事テンプレート整形）
- VOICE-02: リアルタイム文字起こし画面
- VOICE-03: 音声チャットv1（半二重: 録音→STT→LLM→TTS。SPIKE-06の制約リスト準拠）
- MM-01: 画像入力チャット（マルチモーダルモデル）、映像分析（フレーム抽出+一括分析）

## 11. Phase 9: エージェント開発フレームワーク対応（2026-06-12追加・ユーザー指示）

現状のエージェント（AGT-01〜03）はResponses API上のフルスクラッチReActループ。OCI Enterprise AIは**Open Responses Spec互換**により主要エージェントフレームワークをサポートする（公式サンプル: oracle-samples/oci-enterprise-ai — OpenAI SDK / OpenAI Agents SDK / LangChain / LangGraph / AI SDK / CrewAI）。それぞれのフレームワークでの実装例を整備し、フルスクラッチ実装との比較材料（comparison/agent-frameworks.md）を作る。

- FW-01: **OpenAI Agents SDK**版エージェント実装（最優先。handoffs/guardrails等のSDK機能のOCI互換性を実機検証）
- FW-02: LangGraph版（AGT-04のデプロイ済みサンプルを発展。グラフ型オーケストレーション）
- FW-03: その他フレームワーク（CrewAI / AI SDK / LangChain）の互換性検証と比較ドキュメント
- FW-04: フルスクラッチ（AGT-01）との比較整理 → どの顧客類型にどれを薦めるかのプリセールス資料
- 完了条件: FW-01/02のエージェントが本アプリのUIから利用可能。比較ドキュメント完成

## 12. Phase 10: インフラ・アーキテクチャ最適化（2026-06-12追加・ユーザー指示）

ここまでの全ユースケースはContainer Instance 1台（常時起動）のFastAPIに同居している。
**「よりサーバレス・フルマネージド・低コスト」**を目標に、エンドポイントの適材適所を見直す。
ADR-0005の方針（非ストリーミング=OCI Functions / SSE系=Container Instances）を実装に反映する段階。

前提として実装済みの制約に注意:
- SSE系（チャット/議事録生成/STTイベント）はFunctions不可（応答6MB・同期300s・ストリーミング非対応 — ADR-0005）
- リアルタイムSTTセッションは**プロセス内状態**（VOICE-02）→ 常駐プロセス必須。外部化（Redis等）はコスト増と相殺で要比較
- ADB接続はmTLSウォレット+接続プール → Functionsのコールドスタートとプール戦略の実測が必須

- ARCH-01: **現状棚卸しと方式比較**。全エンドポイントをSSE要否/実行時間/状態依存/呼び出し頻度で分類し、
  Functions / Container Instance / その他の適材適所を `docs/comparison/compute-architecture.md` に整理
  （**月額コスト試算つき**: CI常時起動 vs Functions従量 vs 併用。プリセールス転用可能な粒度）
- ARCH-02: 非ストリーミングAPIのFunctions移行（`packages/api/fn` スケルトン活用、API GWルート分割、
  コールドスタート/ADB接続レイテンシの実測、ウォレット取得戦略）
- ARCH-03: 常駐が必要な系（SSE/STTセッション）の最適化（CIの右サイズ、夜間停止運用との整合、
  障害時自動復旧。OKE等への載せ替えは「フルマネージド志向」に反するため原則対象外 — 比較資料には含める）
- ARCH-04: 移行後の実測検証とコストレポート（移行前後の月額比較・レイテンシ比較を定量で）
- 完了条件: 比較ドキュメント完成、移行対象のFunctions実機稼働、コスト削減効果の定量レポート

## 13. Phase 11: エンタープライズ硬化

- SEC-01: SAMLフェデレーション手順書（Entra ID / Google Workspace。Entra ID側は人間作業、手順書をエージェントが起草し人間が実機検証）
- SEC-02: ガードレール（入出力モデレーション設定、監査ログ: 誰が・どのユースケース・どのモデル・トークン数）
- SEC-03: IP制限・レート制限（API GWポリシー/WAF）
- OPS-01: 管理ダッシュボード（利用状況・モデル別トークン・概算コスト）
- OPS-02: 可観測性（OCI Logging/APM計装。オプション: OTel + Langfuseデプロイガイド）

## 14. Phase 12: 配布・ドキュメント

- DOC-01: README日英、アーキテクチャ図（Mermaid + 画像）、デプロイガイド2系統
- DOC-02: AWS版参考実装との機能比較表（できる/できない/OCIだけの強み。四半期更新の定点文書）
- DOC-03: カスタマイズガイド（ユースケース追加、ツール追加、branding.json差し替え、モデル追加）
- DOC-04: presalesデモシナリオ集（社内文書RAG / 売上NL2SQL / 議事録 / MCP連携の4本、台本+所要時間付き）

---

## 15. 人間チェックポイント一覧（エージェントが停止して承認を待つ地点）

| # | タイミング | 判断内容 |
|---|---|---|
| ① | Phase 0完了 | アーキ確定、スコープ修正、SSE経路、RAG主バックエンド、UIルック承認 |
| ② | Phase 2完了 | コアチャットの操作感、性能、履歴設計 |
| ③ | Phase 5完了 | 顧客デモ解禁判断（NL2SQL品質） |
| ④ | Phase 8完了 | セキュリティレビュー、OSS公開可否（社内手続き） |
| 随時 | Terraform apply / IAM変更 / 課金リソース作成 | 都度承認 |

## 16. タスクチケットの書式（specs配下に置くテンプレート）

```
# [TASK-ID] タイトル
## 目的
## 仕様参照: specs/xx-yyy.md §n
## 前提（依存タスク / 人間の事前作業）
## 作業内容（箇条書き）
## 完了条件（検証可能な形で。実機確認の方法を明記）
## 成果物（コード / docs/verification/レポート / ADR）
## 禁止事項（例: 認証情報のコミット、spike外リソースの削除）
```

## 17. スケジュール目安

| フェーズ | 期間目安 | 備考 |
|---|---|---|
| Phase 0 スパイク | 1.5〜2週 | SPIKE-01〜07は並列可（02はインフラ依存で後半） |
| Phase 1 基盤 | 1.5週 | |
| Phase 2 コアチャット | 2週 | |
| Phase 3 UCエンジン | 2週 | |
| Phase 4 RAG | 2週 | |
| Phase 5 NL2SQL | 2週 | チェックポイント③で顧客デモ解禁 |
| Phase 6 エージェント | 3週 | |
| Phase 7 音声/MM | 2週 | |
| Phase 10 インフラ最適化 | 1.5週 | |
| Phase 11 硬化 | 2週 | |
| Phase 12 配布 | 1週+継続 | |

合計 約17〜19週（エージェント主体・人間はレビューとOCI側作業のみの前提）。
最短で顧客に見せられるのは Phase 5 完了時点（約11週）。

---

## 18. Phase 13: デモ生成プラットフォーム化（2026-06-25 追加・ユーザー指示）

正本: `docs/enhance/202607-demo-platform-plan.md`（ヒアリング設計: `docs/enhance/202607-hearing-flow.md`）。
JetUse（=機能カットのリファレンスアーキ）を固定基盤に、フィールドSAがヒアリング駆動で
OCIリファレンスから外れないAIデモを組める基盤へ拡張する。3層プラグイン（L1宣言型/L2 MCP/L3ホスト型）
＋中央レジストリ（ベンダー運用）＋Platform APIブローカー。**IaC生成はしない**（デプロイ上限=コンテナ、Galley不要）。
ガバナンスは「固定基盤＋制約付きビルダー＋合成バリデーション＋コンテナ上限」の4制約で担保。

- **ステージ1（MVP）**: 宣言型プラグインの公開/インストール（PLG-01..08）＋コアサンプル業務アプリ（SBA-01..05 = 問い合わせ/在庫照会/営業案件/帳票OCR）
- **ステージ2**: ヒアリング駆動スタンダードモード（HBD）
- **ステージ3**: コネクタ（Slack）＋Platform APIブローカー
- **ステージ4**: コンテナデプロイ（L3）＋マーケット拡張＋既存資産オンボード（No.1-* / 伝ぴょん）
- **ステージ5**: アドバンスドモード（codex背後・loop-engineering流用）＋フロント生成（Stitch風・ガバナンス付き）
- 関連ADR: ADR-0013（基盤）/ ADR-0014（Platform API）/ ADR-0015（L3）
- タスク: `tasks/PLG-*.md` / `tasks/SBA-*.md`（索引: `tasks/README-demo-platform-s1.md`）
