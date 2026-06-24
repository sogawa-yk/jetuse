# OCI アーキテクチャ構成図（JetUse）

OCI版 JetUse プロトタイプ「JetUse」で使われている OCI サービスを**ざっくり**俯瞰するための構成図。

- **構成図**: [`jetuse-oci-architecture.drawio`](./jetuse-oci-architecture.drawio) — [draw.io](https://app.diagrams.net/) で開く（OCI公式アイコン使用）。
- 詳細な粒度別アーキテクチャ・データフロー・設計判断は [`./system.md`](./system.md)（Mermaid版・正本）を参照。

## 使われている OCI サービス一覧

| 分類 | サービス | 役割 |
|---|---|---|
| 入口 | **API Gateway** | HTTPS入口・JWT通過・パスルーティング（SSE最大300s） |
| 配信/保存 | **Object Storage** | SPA静的配信（PAR）／RAG原本・ウォレット・音声データ |
| API実行 | **Container Instances** | FastAPI本体（SSEチャット・STT中継・大容量UL・OCR） |
| API実行 | **Functions** | 短時間API（presets / dbchat / tts） |
| AI | **Generative AI** | LLM・埋め込み・ガードレール（OpenAI互換API） |
| AI | **GenAI Hosted Applications** | ホスト型エージェント（OpenAI Agents SDK / ADK / LangGraph） |
| データ | **Autonomous Database 26ai** | 会話/定義/議事録/データセット・Select AI・SQL Search |
| 検索 | **Search with OpenSearch** | RAGバックエンドの一つ（k-NNベクトル検索） |
| 音声 | **OCI Speech** | STT（Whisper、大阪）＋ TTS（Phoenix・クロスリージョン） |
| 言語 | **OCI Language** | 翻訳（任意） |
| 文書 | **Document Understanding** | OCR / 文書認識 |
| 認証 | **Identity Domain** | OIDCログイン（PKCE）＋ OAuth（client_credentials） |
| 基盤 | **Container Registry (OCIR)** | API/Functions/エージェントのコンテナイメージ |
| 可観測性 | **Logging / Monitoring** | ログ・メトリクス |
| NW | **VCN / Internet・NAT・Service Gateway** | public（API GW）/ private（CI・Functions・OpenSearch） |

> 図はインフラ目線（どのサービスが何処に配置され、誰が誰を呼ぶか）。機能 × サービスの対応は `./system.md` §1 を参照。

## ユースケース別 構成図

各ユースケースの**実際の呼び出し順序**を、方向付き矢印＋ステップ番号で示した図（`usecases/`）。フローはバックエンド実装（`packages/api/jetuse_core/`）を読み取って作成。`.drawio`（編集可）と `.png`（プレビュー）をペア配置。

| ユースケース | 呼び出しシーケンス（要約） | 図 |
|---|---|---|
| チャット | CI: ①ADBから履歴ロード → ②Generative AI でLLM生成(SSE) → ③ADBへ保存。短期メモリ=OCI Conversations API | [chat](./usecases/usecase-chat.png) |
| ユースケース(プロンプトテンプレート) | チャットと同じ。CIが ADB のユースケース定義をテンプレ展開して system 化 | [usecase](./usecases/usecase-usecase.png) |
| RAGチャット（rag_backendで3方式切替） | A)Vector Store: CI→Generative AI(file_search内蔵) / B)Select AI RAG: CI→ADB(narrate) / C)OpenSearch: CI が 埋込→k-NN→生成 をオーケストレーション | [rag](./usecases/usecase-rag.png) |
| DBチャット (NL2SQL) | CI: ①Generative AI で NL→SQL生成 → ②ADB で SQL実行(読取専用) → ③Generative AI でグラフ提案 | [dbchat](./usecases/usecase-dbchat.png) |
| エージェント（完全hosted化） | CI: ①Identity DomainでOAuth → ②Hosted App を invoke。ツール(LLM/query_database/web)は**Hosted App内**で実行 | [agent](./usecases/usecase-agent.png) |
| 議事録 | CI: ①Object Storageへ音声UL → ②OCI Speech(STTバッチ, Object Storage入出力) → ④ADB保存 → ⑤Generative AIで整形 | [minutes](./usecases/usecase-minutes.png) |
| リアルタイム翻訳 | CI: ①OCI Speech(STTリアルタイム/WebSocket)で確定字幕 → ②Generative AI または OCI Language で翻訳 | [realtime](./usecases/usecase-realtime.png) |
| 音声チャット | ①CI→OCI Speech(STT) → ②CI→Generative AI(LLM) → ③API GW→Functions → ④Functions→OCI Speech TTS(us-phoenix-1) | [voicechat](./usecases/usecase-voicechat.png) |
| 映像分析 | CI: ①Generative AI(vision) に画像+プロンプトを送り分析 | [video](./usecases/usecase-video.png) |
| OCR / 文書認識（engineで2方式切替） | CI: engine=DU→Document Understanding / engine=VLM→CIで画像化してGenerative AI(vision) | [ocr](./usecases/usecase-ocr.png) |

凡例: 実線=主経路 / 破線=任意・補助・取得系。丸数字=実行順序。

> いずれも入口は API Gateway、認証は Identity Domain（OIDC）、SPA配信は Object Storage が共通基盤（各図では当該ユースケース固有のサービスのみ描画）。
> ルーティング(`/api/tts`・`/api/dbchat/*` は Functions、SSE系は Container Instance)は `./system.md` のAPI Gatewayルーティング表に準拠。
> PNGは `.drawio` を編集後 `xvfb-run -a drawio --no-sandbox -x --crop -f png --scale 2 -o <名>.png <名>.drawio` で再生成（`--crop` で余白を自動カット）。
