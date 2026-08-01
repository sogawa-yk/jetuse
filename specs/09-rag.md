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
- アップロードフロー: multipart受信（**20MB上限、拡張子 pdf/txt/md/xlsx のみ**。docxは「未対応(SPIKE-03)」を明示エラー）→ Object Storageへ原本バックアップ（`{RAG_BUCKET}/rag/{owner}/{file_id}_{filename}`、ベストエフォート）→ Files API（purpose=assistants）→ `vector_stores.files.create`（ファイル単位）→ ADB記録（status=processing）
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
- 属性は**ファイル単位**（①-a）。チャンク単位の出典が要る文書は 1 チャンク = 1 ファイルで取り込む
  （**xlsx ではこの回避策を採らない** — PREP-01。セル単位の出典が要るなら `adb` バックエンドを使う）。

### [PREP-01] xlsx の前処理（セル位置つき抽出）2026-07-30追記

対応形式と**出典の粒度**。粒度はバックエンドで違い、その差は隠さない（ADR-0020 の決定内容そのもの。
可視化は RAGM-03 の担当）。

| 形式 | `adb` の出典 | `vector_store`（マネージド）の属性 |
|---|---|---|
| txt / md | チャンク単位（行範囲 `L12:L20`） | **ファイル単位**（1 ファイル 1 種類） |
| pdf | チャンク単位（`p.3` + 行範囲） | **ファイル単位** |
| **xlsx** | **チャンク単位（シート名 + セル範囲 `C5:E5`）** | **ファイル単位**（`sheet` / `cells` は「そのファイル全体」を表す値） |

- 抽出は `jetuse_core/extract_xlsx.py`。**openpyxl を `read_only=True, data_only=True`** で開く
  （大きなブックを一度に展開しない／数式ではなく値を取る。キャッシュの無い数式セルは空扱い）。
  シートごとに**連続する非空セルの矩形**（空行・行の飛びが区切り）を 1 チャンクにし、
  `sheet`（シート名）と `cells`（A1 形式）を付ける。テキスト化は行ごとにタブ区切り。
  結合セルは左上だけが値を持つ（**意味の解釈・ヘッダ推定はしない**）。
- 上限（超過は**切り詰めずに 422**。detail に `limit=<名前>` を書く）:
  ブック 10MB（`workbook_bytes`。xlsx は zip なので汎用 20MB より手前で止める）/
  展開後 100MB（`uncompressed_bytes`。zip の各エントリの合計を**開く前**に見る）/
  走査セル 5,000,000（`scanned_cells`。**空行・空セルも数える** — `read_only` の反復は
  欠けた行を空行で埋めるので、非空行だけ数えると「中身が無いのに広いブック」が上限を抜ける）/
  総チャンク 1,000（`chunks`）/ 1 チャンク 2,000 文字（`chunk_chars`。
  埋め込み API の切り詰め位置に合わせる。上限に収まらない塊は**行境界で分割**し、
  1 行だけで超える場合のみ拒否）。
  **行もチャンクも逐次処理し、上限は 1 件作るたびに見る**（塊を丸ごとメモリに溜めない）。
  壊れたブック（zip は正しいがシート XML が壊れている等）は、開く時だけでなく
  **行を読んでいる最中の例外も** `UnsupportedWorkbook` に正規化して 422 にする。
- `vector_store` への取り込みは、抽出テキストを `<原名>.xlsx.txt` として Files API へ渡す
  （マネージド側は Office 形式を受け付けない — SPIKE-03 の docx と同じ。実測は `docs/verification/PREP-01.md`）。
  台帳の表示名・原本バックアップ・`sha256` は**元の xlsx** のまま。
  属性の `sheet` / `cells` は単一シートなら外接範囲、複数シートなら `(ブック全体: N シート)` / `(ブック全体)`。
  **1 チャンク = 1 ファイルに割って「セル単位で返る」ように見せる細工はしない。**
- `kind` は**両バックエンドに同じ値**を入れる（マネージドの属性と ADB の `kind` 列）。
  そのため `kind` は **UTF-8 で 32 バイト以内の文字列のみ**（ADB 側が `VARCHAR2(32)`。
  BYTE セマンティクス想定なのでバイト長で見る）。**取り込み・検索とも**この制約で、
  数値・真偽・長さ超過は **422**（RAGM-04。以前は非文字列を受けてマネージド `0` /
  ADB `"0"` と入り、同じ絞り込みがバックエンドで違う結果になっていた）。
  **黙って文字列化はしない** — 変換すると入れた値と検索条件が食い違い、静かに 0 件になる。
- `sheet` のファイル単位属性 `(ブック全体: N シート)` の **N は本文があったシート数**
  （空シートは数えない）。
- **`select_ai` の xlsx は「取り込める」**（[PREP-02] で実測。下記）。
- `POST /api/extract`（新規）: ファイルを渡すと**取り込まずに**抽出結果
  `{filename, chunk_count, chunks:[{sheet, cells, text}]}` を返す。案件側が独自の構造化を挟むための口。
  認証・拡張子・サイズ上限はアップロードと同じ。返るチャンクは `adb` へ投入されるものと同一。

### [PREP-02] バックエンドごとの xlsx の扱い（実測）2026-07-30追記

実測の正本は `docs/verification/PREP-02.md`（実行コマンドと実出力つき）。
**一次資料に xlsx 可否の明記は無い**（Select AI の資料は「PDF, DOC, JSON, XML, HTML など」と
例示し、完全な一覧を Oracle Text の対応形式へ委ねている。そこには Excel for Windows 3.0–2019 が
載るが、`.xlsx` という語での明示は無い）。したがって下表は**実測に基づく**。

| バックエンド | xlsx の取り込み経路 | 実測の結果 |
|---|---|---|
| `vector_store`（マネージド） | アプリが抽出したテキストを `<原名>.xlsx.txt` で渡す | 素の xlsx は 400（PREP-01） |
| `adb` | アプリが抽出（`extract_xlsx`） | チャンク単位でセル範囲つき（PREP-01） |
| `opensearch` | アプリが抽出（`extract_xlsx`） | 単体テストのみ。実機**未確認** |
| **`select_ai`** | **バケットの原本を DB 側（`DBMS_CLOUD_AI`）が読む** | **取り込める**。索引作成は成功し、`$VECTAB` の本文は**読めるテキスト**（日本語も化けない）。検索でも xlsx 内のセル値が引ける |

- Select AI の索引に入る xlsx の本文は、DB 側（Oracle Text）が起こしたテキストで、
  **シート名と A1 形式の行・列見出しを含む**（例: `制約` → `A B C` → `2 最大同時接続数 100 …`）。
  ただしチャンクの粒度は**ファイル単位**（本タスクの架空ブックでは 1 ファイル = 1 チャンク）であり、
  `adb` のようなチャンクごとのセル範囲メタデータは付かない。出典は
  `attributes.object_name` / `location_uri`（＝ファイル）まで。
- したがって **取り込み状況バッジは形式で分岐しない**。`select_ai` は
  「索引（`$VECTAB`）に在れば `indexed` / まだ無ければ `pending`（`refresh_rate` 間隔の同期待ち）」。
  PREP-01 が置いた「xlsx は恒久 `error`」は**実測により撤回した**（`rag.SELECT_AI_EXTENSIONS` は削除）。
- **抽出テキスト（`.xlsx.txt`）を Select AI 用にバケットへ置くことはしない。** 原本が問題なく
  読めている以上、置けば同じ内容が二重に索引され（原本 + `.xlsx.txt`）、削除時の整合も
  2 オブジェクト分に増える。得るものが無く、壊すものがある。

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
- **送信可否は「選択中のバックエンドに取り込めた（`backends[選択中] === 'indexed'`）ファイルが
  1 つでもあるか」で決める**（RAGM-04）。マネージド側の `status === 'completed'` だけで決めると、
  ADB を選んでいても送信可に見える（RAGM03-005）。判定根拠を正すだけで、
  「取り込めたか（状況バッジ）」と「何ができるか（能力表示・RAGM-03）」の区別は変えない。

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
