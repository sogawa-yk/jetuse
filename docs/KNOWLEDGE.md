# 技術ナレッジまとめ（ドメイン別ダイジェスト）

OCI版 JetUse プロトタイプの開発で**実機検証によって得た知見**を、ドメイン別に整理した横断リファレンス。
一次情報は [tips.md](./tips.md)（時系列）・各 [verification/](./verification/) レポート・[comparison/](./README.md#4-比較ドキュメントプリセールス転用可定量比較付き) にあり、本書はそれらを引きやすくまとめたもの（内容の重複は許容）。
全体の地図は [README.md](./README.md)。

> 注意: 値（OCID/エンドポイント実値/モデルの可用性）は時点情報。最新は実機・各レポートで確認すること。

---

## 0. 環境・リージョンの確定事実

- 実行環境: OCI computeインスタンス `dev`（ap-osaka-1）。コンパートメント `jetuse-proto`。
- **大阪(ap-osaka-1)はOpenAI互換 agentic API フル対応**（Responses / Conversations / Files / Vector Stores / File Search / Code Interpreter）。
- 大阪のオンデマンドモデル: gpt-oss-120b/20b, command-a系, gemini-2.5-pro/flash, llama-3.3-70b 等。
  **Grok系・Llama4系は大阪不可**（ADR-0001）。
- ADB: `jetuse-dev-adb`（**23.26.2.2.0 = 26ai**）。夜間停止に巻き込まれ自動再開しないため
  セッション開始時に起動確認（`ops/start-adb-if-stopped.sh`）。
- OCI Speech: STTはWhisperで日本語可。**TTSはPhoenix限定**（クロスリージョン呼び出しは追加IAM不要）。

---

## 1. OCI Enterprise AI（OpenAI互換API）

- 認証は **IAM署名**（`oci-genai-auth` で openai-python に署名注入）。必須ヘッダ: CompartmentId + OpenAi-Project。
- **`/embeddings` は非対応**（400 "Unsupported OpenAI operation"）→ 埋め込みはネイティブSDK
  `generative_ai_inference.embed_text`（cohere.embed-multilingual-v3.0 / 1024次元）を使う。
- Conversationsの履歴圧縮は未文書フラグ `metadata.short_term_memory_optimization`（"true"で長会話の累計入力トークン約42%減）。
- サンプリングパラメータ実機対応: Responses系(gpt-oss)= top_p/max_output_tokens/reasoning.effort。
  Chat系(gemini/llama)= top_p/max_tokens/stop/penalties/seed。**モデル系統で出し分けが必要**。
- **Gemini系は小さいmax_tokensで死ぬ**（思考トークン消費）。実用下限2048でクランプ。
- **gpt-oss-120bはtemperature≧1.9で本文が出ない**（推論暴走）→ UI上限1.5。
- TTFT中央値: llama-3.3-70b 0.17s / gemini-flash 1.04s / gpt-oss-120b 1.26s / gemini-2.5-pro 5.80s。
- 長期メモリ: プロジェクト作成時に `--long-term-memory-config` 必須（後から変更不可）。
  利用は会話metadataの `memory_subject_id`（ADR-0006 / SPIKE-10 / AGT-05）。
- マルチモーダル: gemini系は複数画像OK、**llama-3.2-90b-visionは画像1枚まで**（"At most 1 image"で400, ENH-09）。
- 詳細: [SPIKE-01](./verification/SPIKE-01.md) / [CHAT-04b](./verification/CHAT-04b.md) / [CP2-measurements](./verification/CP2-measurements.md)。

---

## 2. 認証・IAM

- アプリ実行は2系統: **ローカル=ユーザー認証(`~/.oci`)** / **本番CI=リソースプリンシパル(RP)**。
  **両者は権限が別物**。ローカルで動いてもCI(RP)で `404 NotAuthorizedOrNotFound` になる罠が頻発
  （翻訳/OCRで実際に発生）。→ **必ず実機CIで再検証**。
- 機能別の必要ポリシーは [setup/iam.md](./setup/iam.md) に集約。代表例（動的グループ `jetuse-dg`）:
  - `use generative-ai-family`（Enterprise AI / 埋め込み / ガードレール）
  - `use database-tools-family` / `read database-family` / `read autonomous-database-family`（Select AI/SQL Search）
  - `manage ai-service-speech-family` + `read buckets` + tag-namespaces（Speech バッチ）
  - `use ai-service-language-family`（翻訳のOCI Language方式・**任意**。未付与でもLLM方式へ自動フォールバック）
  - `use ai-service-document-family`（OCR）
  - `use log-content` / `use metrics`（可観測性）
- **動的グループのマッチングルール反映は5〜10分**。ホスト型エージェントは `generativeaihostedapplication` /
  `generativeaihosteddeployment` の2タイプ追加が必須（片方だけだとFAILED, AGT-04）。
- **認可不足が INTERNAL_ERROR / "Please retry" に化ける**ことがある（Speechバッチ）。ジョブのcreated-byで切り分け。

---

## 3. RAG・検索

4つのRAGバックエンドを実装・比較（[comparison/rag-backends.md](./comparison/rag-backends.md)）。`/rag` でセレクタ切替、
アップロードは全バックエンドへ同時取り込み、**取り込み状況をバックエンド別に可視化**（ENH-05b）。

| 方式 | 実体 | 速度/特性 | 常設コスト |
|---|---|---|---|
| **Vector Store / File Search** | OCI Generative AI のFiles+Vector Store（OpenAI互換） | 4.3s・ストリーミング・引用UI | なし（実質サーバレス）→ **既定採用** |
| **Select AI with RAG** | ADB 26ai `DBMS_CLOUD_AI`（narrate+ベクトル索引） | 2回目以降2.6s・構造化データと同居 | 稼働中ADB再利用（増分なし）。**反映は最遅**（refresh_rate=60分） |
| **OpenSearch (k-NN)** | OCI Search with OpenSearch | ハイブリッド/全文検索・大規模向き | **常設クラスタ課金（〜$100-150/月〜）** |
| **Agents KB** | Generative AI Agents | 机上比較のみ | — |

実機ハマり所:
- Select AIベクトル索引は **ADB 23ai+必須**（19cは `ORA-20047`）。`DBMS_CLOUD_AI` + `DBMS_CLOUD_PIPELINE` のEXECUTE要。
- 取り込み状況の判定: Vector Store=Files API状態 / OpenSearch=index集約(即時) /
  **Select AI=`{INDEX}$VECTAB` の `attributes.object_name`(="{file_id}_{filename}")** に存在するか。
- **OpenSearchは security_mode=DISABLEDでも9200はTLS** → 平文HTTPだと「Server disconnected」。**https + verify=False**。
- 日本語検索品質はSPIKE-03で10/10。OpenSearch最小クラスタは master memory 16GBが下限割れ→32GBへ。
- 詳細: [SPIKE-03](./verification/SPIKE-03.md) / [RAG-01-02](./verification/RAG-01-02.md) / [SPIKE-08](./verification/SPIKE-08.md) / [RAG-03](./verification/RAG-03.md) / [SPIKE-E2](./verification/SPIKE-E2.md)。

### NL2SQL（[comparison/nl2sql-backends.md](./comparison/nl2sql-backends.md)）
- **SQL Search**（Generative AI Semantic Store/enrich）= 正確（10/10）。**Select AI (NL2SQL)** = 速いが四半期取り違え等が残る(8/10)。
- `RUN_TEAM`/GENERATE等は遅く、既定call_timeout(10s)では `DPY-4024` → 個別に延長。
- 詳細: [SPIKE-04](./verification/SPIKE-04.md) / [SQL-01〜04](./verification/SQL-01.md) / [ENH-01](./verification/ENH-01.md)。

### Trusted Answer Search（ENH-06 / [SPIKE-E3](./verification/SPIKE-E3.md)）
- Oracle DB 26aiの「NL→精選ターゲット」**決定的マッピング**（LLM非使用・AI Vector Search・ハルシネーションなし）。
  Search API = `DBMS_TRUSTED_SEARCH.SEARCH()`。**当ADB Serverlessには未提供（no-go）**。

---

## 4. エージェント

- 実行ランタイムを**3つのSDK別Hosted Application + Select AI Agent**に集約（ADR-0009 / [comparison/agent-runtimes.md](./comparison/agent-runtimes.md)）。
  - OpenAI Agents SDK / Google ADK / LangGraph をそれぞれホスト型ReActコンテナとしてデプロイ。SDK選択でルーティング。
  - ツール・プロンプトは**ステートとして後から送る**（近年のReActの流儀）。
- **OpenAI Agents SDKはResponses直結不可** → `OpenAIChatCompletionsModel` 経由（ADR-0007）。
- **Google ADK 2.2.0はPython 3.12必須**。LiteLlmはper-request IAM署名ができず、カスタムBaseLlmで署名注入（SPIKE-ADK）。
- ツール実機: カスタムfunctionツール完全動作。**code_interpreter built-in動作**。**web_search built-inは不可**（"only supported for OpenAI provider"）→ 検索は自前（DuckDuckGo HTML、APIキー不要）。
- **Select AI Agent**（`DBMS_CLOUD_AI_AGENT`）: CREATE_TOOL/AGENT/TASK/TEAM → RUN_TEAM。
  team名はリテラル（named-bindは ORA-00904）、call_timeout 240s、一覧系は **ROW_GUARD(最大50行)** でHTTP 413回避。
- **Hosted Applicationのイメージ更新は in-place不可** → **アプリ削除→再作成**（OCID更新→tfvars→API再デプロイ）。
- 詳細: [AGT-01](./verification/AGT-01.md) / [AGT-MULTI](./verification/AGT-MULTI.md) / [FW-01](./verification/FW-01.md) / [FW-02](./verification/FW-02.md) / [ENH-04](./verification/ENH-04.md) / [SPIKE-ADK](./verification/SPIKE-ADK.md)。

---

## 5. 音声・映像・翻訳

- **議事録**(VOICE-01): Whisperバッチ+話者分離+LLM整形。バッチは `manage object-family` + tag-namespaces 必要（不足だと INTERNAL_ERROR）。
- **リアルタイムSTT**(VOICE-02): API GWは**WebSocket非対応** → 「音声=チャンクPOST / 結果=SSE」中継。Whisperリアルタイムは**partialなし**（final数秒遅れ）。RPでも動く。
- **音声チャット**(VOICE-03): 半二重（話す→STT→LLM→TTS）。**全二重はOCIにストリーミング対話モデルが無く現状不可**（[SPIKE-G5](./verification/SPIKE-G5.md)）。TTS既定出力はMP3でなくWAV。
- **翻訳**(ENH-10 / [comparison/translation.md](./comparison/translation.md)): LLM(llama-3.3-70b)とOCI Languageの2択。両方大阪可用・低レイテンシ。OCI Languageは `use ai-service-language-family` 要（未付与時はLLMへ自動フォールバック）。
- **映像分析**(MM-01/ENH-09): ブラウザでフレーム抽出→画像のみ送信。複数画像はgemini、llama-3.2-visionは1枚。

---

## 6. OCR / ドキュメント理解（ENH-07 / [guides/ocr-limits-and-workarounds.md](./guides/ocr-limits-and-workarounds.md)）

- **OCI Document Understanding**: 大阪可用・日本語高精度（char recall 100% / mean conf 0.994）。同期API `analyze_document`(inline)。
- **同期APIは最大5ページ**（6ページ以上で413）→ pypdfで5ページ分割→並列OCR→マージで透過対応。
  多ページの直列処理はGWの `read_timeout` 60sを超え504 → **チャンク並列化 + `/api/ocr`ルートを300sに**。
- **テーブル抽出は英語のみ・全リージョン共通**（日本語は0件）。ヘッダーは `header_rows` に入る（`body_rows`だけ読むと欠落）。
- 2エンジン選択式: **Document Understanding**（日本語テキスト高精度・表は英語のみ） / **VLM**（gemini-2.5-pro等のビジョンLLM、**日本語の表も抽出可**、ページ毎LLM呼び出し）。
- IAM: `use ai-service-document-family`（未付与は404→友好的422）。

---

## 7. ガードレール・セキュリティ

- **ApplyGuardrails**: プロンプトインジェクション検知=言語非依存で機能。**コンテンツモデレーションは日本語非対応**（英語のみ）。PIIは既定未検知。→ 日本語アプリではプロンプトインジェクションのみ採用（GAP-01）。
- 入力モデレーション+監査ログ（SEC-02）、IP制限・レート制限（SEC-03 / [comparison/access-control.md](./comparison/access-control.md)）。
- SAMLフェデレーションはOCIマネージド完結で構成可（GAP-02）。
- 詳細: [SEC-02](./verification/SEC-02.md) / [SEC-03](./verification/SEC-03.md) / [GAP-01](./verification/GAP-01.md)。

---

## 8. インフラ・デプロイのハマり所（重要）

- **実行基盤**: Functions優先、**SSEストリーミング経路のみ Container Instance**（ADR-0005）。SSEはAPI GW経由・readTimeout最大300秒（ADR-0003 / SPIKE-02）。
- **CIを `-target` で再作成するとプライベートIPが変わり、API GWのバックエンドが旧IPのまま取り残される**（全リクエスト到達不能=curl 000）。
  → **イメージ更新でCI再作成したら必ずフルの `terraform apply`** でGWのbackend IPまで反映。切り分け: `000`=GW→backend不通(IP不一致)、`401/404`=appまで到達。
- **API GatewayのHTTP_BACKENDは connect/send timeout が >=1必須**。ルート挿入で状態indexがFunctionsルート(0)とずれると400。
- **同じイメージタグだとコード変更が反映されない**（env注入のCI再作成でも）→ コード変更時は必ず再ビルド。OCIR pushはディスク逼迫時に静かに失敗 → apply前にタグ存在確認。
- **ADB接続**: タイムアウト3層（tcp_connect_timeout / pool wait_timeout 15s / call_timeout）必須。
  ウォレットの ewallet.pem はパスワード保護（`ADB_WALLET_PASSWORD`未指定で無限プロンプト→DPY-4005様ハング）。
- **API GWはWebSocket非対応**（HTTP/Sのみ）。
- デプロイ手順は [../README.md](../README.md)（リポジトリ直下）。インフラ詳細は [INFRA-01](./verification/INFRA-01.md) / [ARCH-02-04](./verification/ARCH-02-04.md)。

---

## 9. フロントエンド・UI

- OCIコンソール風（Redwood）デザインシステム（[SPIKE-07](./verification/SPIKE-07.md) / [UI-01-03](./verification/UI-01-03.md)）。テーマは `theme.css` のトークン。
- **Tailwind v4採用**。`translate-x-*` は `transform` ではなく **CSS `translate` プロパティ**を出力し、絶対配置要素の静的位置に加算される（音声トグルのノブはみ出し不具合の原因）→ 位置はみ出しは **明示的 `left`** で実装。
- ルーティングは **HashRouter**（`#/path`）。リブランドは `public/branding.json`（productName/shortName/logoText）+ `index.html` title。
- 配列編集は「カンマ区切り即時split」を避ける。LLM生成mermaidの未クオート括弧は自動クオートで救済。
- 背景テクスチャは `.bg-texture`（multiply）+ 半透明背景色オーバーレイ（`--texture-fade`）で濃さ調整。

---

## 10. 早見表（採用方針）

| 領域 | 既定採用 | 代替・条件付き |
|---|---|---|
| RAG | Vector Store / File Search | Select AI RAG（構造化と融合/低レイテンシ）・OpenSearch（ハイブリッド/大規模・常設費）・Agents KB |
| NL2SQL | SQL Search（正確） | Select AI NL2SQL（高速） |
| エージェント | OpenAI Agents SDK（Hosted） | ADK / LangGraph / Select AI Agent（SDK選択でルーティング） |
| 翻訳 | Enterprise AI LLM | OCI Language（IAM付与時・最速） |
| OCR | Document Understanding | VLM（日本語の表・複雑レイアウト） |
| ガードレール | プロンプトインジェクション検知 | （モデレーションは日本語非対応） |
| 全二重音声 | 半二重（VOICE-03） | 不可（OCIにモデルなし） |
| Trusted Answer Search | （ADB Serverlessに未提供） | AI Vector Searchで自前実装は可能 |
