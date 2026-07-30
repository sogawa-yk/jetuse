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

### [RAGM-01] メタデータ属性（ADR-0020 §1）

- `POST /api/rag/files` の multipart に **`attributes`（JSONオブジェクト文字列・省略可）**。
  許可キーは `file` / `version` / `sheet` / `cells` / `sha256` / `kind` / `current_version` / `chunk_id`
  （`jetuse_core/rag_metadata.py` が単一の門番）。`file` と `sha256` は未指定なら取り込み側で補完。
- **値が無いメタはキーごと省く**（空文字を入れない＝`eq` フィルタが静かに一致しなくなるため）。
  `0` / `False` は値として残す。
- 上限（キー16 / 値512文字 / 入れ子不可 — SPIKE-M1 ①-d）超過と未知キーは **422 で拒否**（切り詰めない。
  値が変わるとフィルタが静かに外れ、「該当なし」と区別できなくなる）。
- 属性は**ファイル単位**（①-a）。チャンク単位の出典が要る文書は 1 チャンク = 1 ファイルで取り込む。

## [RAG-02] RAGチャット

### 設計

- `/api/chat/stream` 拡張: `rag: true` で当該ユーザーのvector_storeを `file_search` ツールに接続（**Responses系=gpt-ossのみ**。他モデル指定時は400）
- instructionsにツール強制文を自動付与（SPIKE-03b文言ベース）。`include=["file_search_call.results"]`
- SSEに **`{"citations": [{filename, file_id, score}]}` イベント**を追加（response.completed時にfile_search_call.results + annotationsから抽出、重複排除）
- [RAGM-01] citations は**追加専用**で拡張（既存3フィールドは温存＝既存フロントは無変更で動く）:
  `source`（取り込み時 attributes 由来の構造化出典）/ `text`（該当箇所の本文・500字で切り詰め）/ `chunk_id`。
  同一ファイルの複数ヒットは最上位スコアのチャンクを代表にする（属性はファイル単位のため）。
- [RAGM-01] `/api/chat/stream` に **`rag_filters`**（`{"type":"eq","key":"current_version","value":"Y"}`
  や `and`/`or` の複合）。`tools[].filters` として渡す。**未知キーは 422**（上流はエラーにせず0件を返すため
  — SPIKE-M1 ①-b）。`in` は上流未対応につき 422。`vector_store` 以外のバックエンドと併用したら 400
  （黙って無視すると旧版が混ざる）。エージェントモード（`agent` / `agent_id`）も同じ理由で 400
  （別ディスパッチで絞り込みを渡す口が無い）。
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
- **ADMINセットアップ（再実行可）**: `ops/setup-select-ai.py [--schema <SCHEMA>]` — 対象スキーマへ `EXECUTE ON DBMS_CLOUD / DBMS_CLOUD_AI` 付与 + GenAI/Object StorageホストへのACL + `DBMS_CLOUD_ADMIN.ENABLE_RESOURCE_PRINCIPAL`。DB内の資格情報は開発も配備も **`OCI$RESOURCE_PRINCIPAL`**（ADB自身の身分）に統一し、APIキーを焼き込む `JETUSE_OCI_CRED` は廃止（ADR-0021）。Vault化はPhase 8の検討事項
- UI: /ragにバックエンドセレクタ（標準=ベクトル検索 / Select AI）。select_ai選択時は同期タイミングの注記表示

### 完了条件

- [ ] 実機: 同一アップロード文書に対し両バックエンドで質問→正答+出典。切替がUIから機能
- [ ] pytest / lint / build

## [RAG-04] RAGバックエンド比較ドキュメント — **完了** → docs/comparison/rag-backends.md（SPIKE-08で定量比較済み）
