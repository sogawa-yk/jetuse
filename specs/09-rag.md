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
- **ADMINセットアップ（再実行可）**: `ops/setup-select-ai.py [--schema <SCHEMA>]` — 対象スキーマへ `EXECUTE ON DBMS_CLOUD / DBMS_CLOUD_AI` 付与 + GenAI/Object StorageホストへのACL + `DBMS_CLOUD_ADMIN.ENABLE_RESOURCE_PRINCIPAL`。DB内の資格情報は開発も配備も **`OCI$RESOURCE_PRINCIPAL`**（ADB自身の身分）に統一し、APIキーを焼き込む `JETUSE_OCI_CRED` は廃止（ADR-0021）。Vault化はPhase 8の検討事項
- UI: /ragにバックエンドセレクタ（標準=ベクトル検索 / Select AI）。select_ai選択時は同期タイミングの注記表示

### 完了条件

- [ ] 実機: 同一アップロード文書に対し両バックエンドで質問→正答+出典。切替がUIから機能
- [ ] pytest / lint / build

## [RAG-04] RAGバックエンド比較ドキュメント — **完了** → docs/comparison/rag-backends.md（SPIKE-08で定量比較済み）

## [RAGM-02] `adb` バックエンド — Oracle AI Database 自前索引（2026-07-30追記 / ADR-0020 §2）

前提: ADB 23ai 以上（`VECTOR` 型）。既存 3 バックエンド（vector_store / select_ai / opensearch）は変更しない。

### 設計

- `/api/chat/stream` の `rag_backend` に `"adb"` を追加（既存の非 Responses 系と同じ枠組み＝自前検索 → 単発 delta）。
- 表 `rag_adb_chunks`（migration `017_rag_adb.sql` は **CREATE TABLE 1 文だけ**）: 本文 `CLOB` +
  メタ列（`doc_file` / `doc_version` / `sheet_name` / `cells` / `sha256` / `kind` / `current_version`）+
  任意メタ `JSON` + `VECTOR(1024, FLOAT32)`。
  **索引は `rag_adb.ensure_indexes()` が冪等に作る**（B木 3 本 + ベクタ索引）。マイグレーションに
  複数 DDL を並べないのは、Oracle の DDL が暗黙コミットで、途中失敗すると「表はあるが migration
  未記録」で再実行不能になるため。**フィルタ列の B木索引が無いと、メタデータ絞り込み付きの
  ベクタ検索の実行計画が安定しない**（素の表では全件走査と `HNSW SCAN IN-FILTER` の両方が出た。
  B木索引 + `owner_sub` 込みで `HNSW SCAN PRE-FILTER` に安定。実測: `docs/verification/RAGM-02.md`）。
- 取り込み: 行を跨がないチャンク化（**チャンクごとに `sheet` と行範囲 `L{開始}:L{終了}` を出典として持つ**）→
  クライアント側埋め込み（`jetuse_core.embeddings`）→ 投入。**DB 内埋め込みは採らない**
  （`OCI$RESOURCE_PRINCIPAL` では ORA-24247。API キーの DB 内資格情報は ADR-0021 で廃止済み）。
  同名ファイルの再取り込みは旧チャンクを `current_version='N'` に落として版を上げる（消さない）。
- 索引は取り込み時に遅延作成（ベクタ索引は HNSW → 不可なら IVF）。作成できなくても厳密検索で
  結果は正しいので取り込みは失敗させない。
- 検索: 「メタデータ絞り込み + ベクタ類似検索」を 1 本の SQL（`FETCH APPROX FIRST` =
  ベクタ索引を使う形。索引が無い / 使えないときは Oracle 側が厳密検索へ落ちる）。
  フィルタは**許可キー制**（`current_version` /
  `version` / `file` / `file_id` / `sheet` / `kind`）で、値は必ずバインドする。未知キーはエラーにする
  （誤字が静かに 0 件になるのを防ぐ）。
- citations: 既存契約 `{file_id, filename, score}` を保ったまま `source`（チャンク単位の出典）と `text` を足す。
- 取り込み状況バッジ: `backends.adb` = `indexed` / `pending` / **`error`** / `disabled`。
  - `indexed`: チャンク表にその `file_id` の行がある
  - `error`: 取り込み状態表（019）が `error`（未対応形式・本文を取り出せない・取り込み時の例外）
  - `pending`: どちらでもない（取り込み前 / 取り込み中）。再取り込みは同じファイルを上げ直す（版が上がる）
  - `disabled`: チャンク表が無い環境（017 未適用 / 23ai 未満）

### 完了条件

- [x] 実機: `rag_backend='adb'` で取り込み → 検索 → 回答。同一ファイルの複数チャンクが別々の `cells` を返す
- [x] 実機: 版フィルタの対照（`current_version='Y'` で旧版 0 件 / フィルタ無しでは返る）
- [x] 実機: 業務表と JOIN したベクタ検索が 1 クエリで成立
- [x] スケール検証（50,000 チャンク・実行計画と再現率）を `docs/verification/RAGM-02.md` に記録
- [x] pytest / ruff
