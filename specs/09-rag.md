# specs/09 — Phase 4 RAG（RAG-01〜04）

状態: ドラフト（2026-06-11作成。RAG-01/02を先行、RAG-03/04は順次追記）
仕様参照: SPIKE-03（Vector Store/File Search実機確定事項）/ specs/07（チャット基盤）

## 前提（SPIKE-03実機確定）

- Vector Store本体CRUD=CPホスト、files等サブリソース=DPホスト（`OpenAi-Project` 必須）
- CP completed後もDP伝播に10〜30秒。**docx非対応**（unsupported_file）。バッチは1ファイル失敗で全体400→**ファイル単位で取り込み**
- file_searchは**instructionsでツール使用を強制**しないとモデルが一般論で答える（強制で10/10、なしで7/10）
- 引用は `include=["file_search_call.results"]` + message annotations

## [RAG-01] ファイル管理

### 設計

- **ベクトルストアはユーザーごとに1つ**を遅延作成（`RAG_STORES(owner_sub PK, vector_store_id)`、migration 005）。共有ナレッジベースはPhase 4出口で要否判断
- `RAG_FILES(id PK, owner_sub, filename, oci_file_id, status, bytes, error, created_at)` — 表示名と状態の正はADB
- アップロードフロー: multipart受信（**20MB上限、拡張子 pdf/txt/md のみ**。docxは「未対応(SPIKE-03)」を明示エラー）→ Object Storageへ原本バックアップ（`{RAG_BUCKET}/rag/{owner}/{file_id}_{filename}`、ベストエフォート）→ Files API（purpose=assistants）→ `vector_stores.files.create`（ファイル単位）→ ADB記録（status=processing）
- 状態: 一覧取得時にprocessingの行だけDPへ `files.retrieve` して completed/failed を反映
- 削除: VSから除去→Files API削除→OS原本削除（ベストエフォート）→ADB削除

### API

- `GET /api/rag/files` / `POST /api/rag/files`（multipart）/ `DELETE /api/rag/files/{id}`
- 依存追加: `python-multipart`（FastAPIのUploadFile要件）

## [RAG-02] RAGチャット

### 設計

- `/api/chat/stream` 拡張: `rag: true` で当該ユーザーのvector_storeを `file_search` ツールに接続（**Responses系=gpt-ossのみ**。他モデル指定時は400）
- instructionsにツール強制文を自動付与（SPIKE-03b文言ベース）。`include=["file_search_call.results"]`
- SSEに **`{"citations": [{filename, file_id, score}]}` イベント**を追加（response.completed時にfile_search_call.results + annotationsから抽出、重複排除）
- 会話はクライアント保持の全履歴再送（ステートレス）。ADB永続化はPhase 4出口で要否判断

### UI（/rag）

- 左: ファイル一覧（アップロード、状態バッジ processing/completed/failed、削除）
- 右: チャット（ストリーミング・停止・Md描画）。アシスタント応答下に**引用元チップ**（ファイル名+スコア）
- ファイル0件時はアップロード誘導を表示

### 完了条件

- [ ] pytest / lint / build
- [ ] 実機: アップロード→completed→RAG質問が文書内容で回答+引用元表示→削除→検索結果から消える
- [ ] docxアップロードが明示エラー、21MB超が413相当

## [RAG-03] Select AI RAGバックエンド切替（2026-06-11追記）

前提（SPIKE-08 + ユーザー承認）: Select AIベクトル索引はADB 23ai+必須。jetusedevは**26aiへスケジュールアップグレード**（OCIの19cからのアップグレード先は26ai。23ai機能を包含）。

### 設計

- `/api/chat/stream` に `rag_backend: "vector_store" | "select_ai"`（既定 vector_store）。select_aiはモデル選択不可（プロファイルのLLM=llamaを使用）
- **per-user分離**: ユーザーごとに `JETUSE_RAG_{sha1(owner)[:8]}` のprofile+vector indexを遅延作成。索引の取り込み元は **RAG-01の原本バックアップ先 `rag/{owner}/`**（同じアップロードが両バックエンドに供給される設計）
- 実行: `DBMS_CLOUD_AI.GENERATE(action=>'narrate')` をスレッドで実行→単発deltaで返す（非ストリーミング）。応答末尾のSourcesをcitationsイベントに変換
- 同期の制約: select_ai側は索引の `refresh_rate`（60分に設定）間隔でバケットと同期。アップロード直後は反映されない場合がある旨をUIに注記
- **ADMINセットアップ（1回）**: `ops/setup-select-ai.py` — JETUSE_APPへ `EXECUTE ON DBMS_CLOUD / DBMS_CLOUD_AI` 付与 + GenAI/Object StorageホストへのACL + APIキーcredential作成（Vault/リソースプリンシパル化はPhase 8）
- UI: /ragにバックエンドセレクタ（標準=ベクトル検索 / Select AI）。select_ai選択時は同期タイミングの注記表示

### 完了条件

- [ ] 実機: 同一アップロード文書に対し両バックエンドで質問→正答+出典。切替がUIから機能
- [ ] pytest / lint / build

## [RAG-04] RAGバックエンド比較ドキュメント — **完了** → docs/comparison/rag-backends.md（SPIKE-08で定量比較済み）
