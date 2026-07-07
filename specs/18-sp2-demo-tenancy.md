# specs/18: SP2 — テナンシ + Demo エンティティ 詳細仕様

> 状態: ドラフト（人間レビュー待ち — SP2-00 の人間ゲート）。日付: 2026-07-06。
> 上位: specs/17-demo-platform-redesign.md §5・§6 / ADR-0015・ADR-0016。
> 前提実装: SP1-01（能力カタログ）・SP1-02（demos 最小レジストリ + DemoContext seam）・
> SP1-03（デモスコープ chat/rag 縦切り）。検証レポート: docs/verification/SP1-0{1,2,3}.md。
> 比較ドキュメント: docs/comparison/demo-box-provisioning.md（§3 の決定根拠）。

## 0. 位置づけ・スコープ

SP2 は Internal 固有（`dev` 枝 — specs/17 §7）。specs/17 §6 の概略
「`Demo` を一級化し、箱をプロビジョニングし、`DemoContext` の解決先を実装し、
Identity Domains でユーザー分離する」を実装可能な粒度に確定する。

| タスク | 本仕様の対応節 |
|---|---|
| SP2-01 Demo エンティティ本格化 + CRUD | §1・§2 |
| SP2-02 箱のライフサイクル（lazy 解決・削除後始末・**VPD 基盤**） | §3・§4.3 層1（基盤 — codex review-7 B001: registry-first 作成が VPD 付与に依存するため基盤は SP2-02 が所有） |
| SP2-03 DemoContext 解決先 + dbchat デモスコープ化 | §4（VPD 基盤は SP2-02 のものを利用） |
| SP2-04 Internal テナンシ分離（Identity Domains 実接続） | §5 |

**スコープ境界**: ビルダー（SP3）・マーケットプレイス（SP4）・`connector.invoke`・
統一 Capability インターフェースは対象外。Public（main）の user 単位ルートのパス・挙動は変えない。

## 1. Demo エンティティ完全形

### 1.1 スキーマ（migration 017〜021 — **1 ファイル = 1 文**）

migrate.py は**ファイル単位**でしか適用を記録せず、Oracle の DDL は暗黙 commit されるため、
複文ファイルは途中失敗で「前半だけ適用済み・再実行は ORA-01430/ORA-00955 で停止」になる
（codex review-1 B002）。よって SP2 のスキーマ変更は**単文 migration 5 ファイル**に分割する。
列追加は 1 つの `ALTER TABLE ... ADD (...)` に束ねて原子化する。
**加えてランナー自体の再実行許容を SP2-01 で追加する**（codex review-8 B001 —
単文化しても「DDL 成功 → version 記録前のクラッシュ」で記録なしの適用済み DDL が残り、
再実行が停止する）: migrate.py は既適用を示唆する ORA コード（ORA-01430 / ORA-00955 /
ORA-01408 等）を検知したら、**ORA コードだけで成功と断定せず、その migration の期待事後条件を
データディクショナリで検証**（USER_TAB_COLUMNS の列・型・DEFAULT / USER_CONSTRAINTS /
USER_INDEXES・USER_IND_COLUMNS が期待形と**完全一致**）し、一致した場合のみ version を
記録する（codex review-9 B001 — 同名で形の違うオブジェクトを適用済みにしない）。
不一致は停止して人間対応。fault-injection テスト（各 migration の DDL 成功直後・
version 記録前で停止 → 再実行で収束。**部分一致・形違いのオブジェクトでは停止**）を
017〜021 に対して行う:

| ファイル | DDL（単文） |
|---|---|
| `017_demos_v2.sql` | `ALTER TABLE demos ADD (description VARCHAR2(1000 CHAR), config CLOB DEFAULT '{}' NOT NULL CHECK (config IS JSON), status VARCHAR2(20) DEFAULT 'ready' NOT NULL CHECK (status IN ('provisioning','ready','failed','deleting')), updated_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL)` |
| `018_demos_idx_owner.sql` | `CREATE INDEX idx_demos_owner ON demos(owner_sub, updated_at)` |
| `019_demos_idx_visibility.sql` | `CREATE INDEX idx_demos_visibility ON demos(visibility)` |
| `020_conversations_demo_id.sql` | `ALTER TABLE conversations ADD demo_id VARCHAR2(36)` |
| `021_conversations_idx_demo.sql` | `CREATE INDEX idx_conv_demo ON conversations(demo_id)` |

016_demos.sql（SP1-02: id / owner_sub / name / visibility / created_at）からの差分の根拠:

| 列/索引 | 根拠 |
|---|---|
| config | SP3 ビルダー成果物（能力配線・UI メタ）の置き場。SP2 では**不透明な JSON オブジェクト**として保存・返却のみ。形式検証は DB ネイティブ制約（IS JSON） |
| status | §1.2 の状態機械。ライフサイクルは SP2 が所有するため DB CHECK で値域を固定 |
| description | usecases 同型（SP3 が説明を保存、SP4 マーケットの表示項目）。CHAR セマンティクス（日本語の ORA-12899 回避 — SP1-02 DB-001 と同じ理由） |
| updated_at | usecases 同型。更新は**全 UPDATE 文で `SET updated_at = SYSTIMESTAMP`**（usecases.py の流儀。トリガ不使用） |
| idx_demos_owner / idx_demos_visibility | usecases の索引パターン踏襲（一覧 = owner + updated_at DESC、SP4 の公開一覧 = visibility） |
| conversations.demo_id + idx_conv_demo | specs/17 §5「会話 → demo_id で紐付け」。利用は §4.2（SP2-03）だが、スキーマ変更は SP2-01 の migration 群に集約 |

- `visibility` の値域は `'private' | 'public'`（既定 private）。**DB CHECK は付けない**
  （usecases と同じく API 層で検証。SP4 で `'published'` を追加予定のため値域が伸びる）。
- FK（conversations.demo_id → demos.id）は張らない: 削除後始末は §3.2 の順序制御で行い、
  ON DELETE CASCADE で外部リソース（vector store 等）の後始末と DB だけが乖離するのを避ける。

### 1.2 status 状態機械

```
（POST /api/demos）──▶ ready ──（DELETE 受理）──▶ deleting ──（後始末完走）──▶ 行削除
                       ▲                            │
   （SP3: ビルダー産デモ）│                            └─（途中失敗）─▶ deleting のまま
provisioning ──────────┴──▶ failed ──（DELETE 受理）──▶ deleting        （再 DELETE で収束 §3.2）
```

- **SP2 の生成は即 `ready`**（箱は lazy — §3.1。POST は INSERT のみで外部リソースを作らない）。
- `provisioning` / `failed` は SP3 予約（ビルダーのデータ生成が非同期に走る間の状態）。
  SP2 のコードはこの 2 状態を**生成しない**が、値域・seam の扱い（§2.3）は先に確定しておく。
- `failed → ready`（再プロビジョニング）は SP3 で定義。SP2 では遷移させない。
- **status は API から直接変更不可**（PATCH の入力スキーマに含めない。サーバ管理列）。
- 遷移の実装は楽観条件付き UPDATE（`WHERE id=:id AND status=:from`）で競合遷移を防ぐ。

### 1.3 リポジトリ層（jetuse_core/demos.py 拡張）

usecases.py の流儀（所有者強制は SQL の WHERE 句）を踏襲:

- `create_demo(owner, name, description=None, visibility='private', config=None)` — 既存拡張。
- `list_demos(owner)` — 自分の所有のみ、`updated_at DESC`。
- `update_demo(owner, demo_id, fields)` — name/description/visibility/config の部分更新。
  `WHERE id=:id AND owner_sub=:o`。0行更新なら None（ルート側 404）。
- `set_status(demo_id, from_status, to_status)` — §1.2 の楽観遷移。
- `get_demo(demo_id)` — 既存のまま（生取得。認可は require_demo 側 — SP1-02）。

## 2. CRUD API 契約（/api/demos 配下）

### 2.1 ルート一覧

| Method/Path | 認可 | 成功レスポンス | 備考 |
|---|---|---|---|
| `GET /api/demos` | `require_user` | `{"demos": [DemoOut]}` | **自分の所有のみ**（updated_at DESC）。公開デモの横断一覧は SP4（マーケット）まで作らない |
| `POST /api/demos` | `require_user` | `DemoOut` | Body = DemoCreate（§2.2）。INSERT のみ・即 `status='ready'`（§3.1） |
| `GET /api/demos/{demo_id}` | `require_demo` | `DemoOut` | 公開デモは非所有者も 200 |
| `PATCH /api/demos/{demo_id}` | `require_demo_owner` | `DemoOut` | 部分更新: name/description/visibility/config。id/owner_sub/status は変更不可（入力スキーマに含めない） |
| `DELETE /api/demos/{demo_id}` | 所有者のみ（§2.3 注記） | `{"deleted": true}` | 後始末は §3.2。`status='deleting'` の残骸にも受理（再実行 = 収束）。**ルートの初公開は SP2-02**（下記） |

usecases のルート流儀（`{"usecases": [...]}` / `{"deleted": true}` / `mine`）と同語彙に揃える。

**DELETE は SP2-02 まで公開しない**（codex review-2 B001）: SP1-03 時点でデモスコープ RAG upload が
使えるため、後始末を持たない行削除だけの DELETE を先に出すと、store / files / 原本 / 索引を持つデモの
demo 行だけが消えて資源が孤児化し、SP2-02 導入後も辿れない。SP2-01 は CRUD 4 ルート
（GET 一覧 / POST / GET / PATCH）で、DELETE は §3.2 の後始末込みで SP2-02 が初めて公開する。

### 2.2 スキーマ

**DemoCreate**（POST）/ **DemoPatch**（PATCH）:

- **PATCH の null 意味論**（codex review-2 M004）: 「フィールド省略」と「明示 null」を区別する
  （pydantic `exclude_unset`）。省略 = 変更しない。明示 null は `description` のみ許可（クリア）。
  `name` / `visibility` / `config` への明示 null は **422**（DB 上 NOT NULL — Oracle エラーを
  500 で漏らさない）。空 PATCH（全省略）は 200 で現状を返す（updated_at も変えない）。

| フィールド | 型・制約 |
|---|---|
| `name` | str、1〜200 文字（必須は POST のみ） |
| `description` | str、≤1000 文字、省略可 |
| `visibility` | `'private'`（既定）\| `'public'`。API 層で Literal 検証 |
| `config` | JSON オブジェクト（dict）。既定 `{}`。**直列化後 ≤1MB**（超過は 422 — 信頼境界の入力上限）。SP2 では原則不透明だが、**`config.dbchat.model` のみ SP2-03 が解釈する正規キー**（codex review-10 M004 / review-11 M003）: `dbchat` キーが存在する場合は object 必須（文字列・配列・null は 422）、その中の `model` が存在する場合は str + Select AI モデル一覧（`resolve_select_ai_model` の allowlist）必須（null・型違い・未知値は 422）。**POST / PATCH とも同一の検証**。省略/欠落時は既定モデル。他キーは検証せず保存・返却のみ。境界テスト: dbchat の null/配列/文字列・model の欠落/null/未知値 |

**DemoOut**:

```json
{"id": "...", "name": "...", "description": null, "visibility": "private",
 "status": "ready", "config": {}, "created_at": "...", "updated_at": "...", "mine": true}
```

- **owner_sub は返さない**。公開デモを非所有者が GET したとき IdP の sub（ユーザー識別子）が
  漏れるのを避け、フロントの要件（編集可否の判定）は `mine`（usecases と同語彙）で満たす。

### 2.3 認可・存在秘匿（SP1 踏襲 + deleting の扱い）

- 使い分けの原則（SP1-03 で確立済み）: **読み取り = `require_demo`**（公開デモは非所有者も可）、
  **書き込み = `require_demo_owner`**、**一覧・作成 = `require_user`**。
- 存在秘匿 404: 「存在しない id」「他人の private」「`status='deleting'`」は**同一の
  404 `{"detail": "demo not found"}`**。403 は使わない（SP1-02 の fail-closed 設計）。401 は require_user。
- **`require_demo` の変更点**: `status='deleting'` を 404 に含める。解体中の箱への能力呼び出しが
  lazy 生成（ensure_store 等）で箱を**復活させる事故を構造的に封じる**（§3.2 手順1と対）。
  あわせて `DemoContext` に `status` フィールドを追加する。
- **DELETE だけの例外**: 所有者の DELETE は `status='deleting'` でも受理する必要がある
  （後始末途中失敗の再実行 — §3.2）。DELETE ハンドラは require_demo を経由せず
  `demos.get_demo` + 所有者検証（不一致・不存在は同一 404）で受ける。存在秘匿の挙動は他と同一。

## 3. 箱のプロビジョニング方式

### 3.1 決定: 論理名前空間 + lazy 生成（比較: docs/comparison/demo-box-provisioning.md）

**「demo_<id> スキーマ」は物理 Oracle スキーマ（CREATE USER）ではなく論理名前空間**とする。
既存資産の owner キーに `DemoContext.namespace = "demo_<uuid>"` を差すだけで箱が分かれる:

| 箱 | 実体 | 生成タイミング |
|---|---|---|
| RAG | `rag_stores` / `rag_files` の `owner_sub = namespace` 行 + デモ専用 vector store | **lazy** — 初回ファイルアップロード時に `ensure_store(namespace)`（SP1-03 で有界リトライ込み実機確認済み・初回 44 秒） |
| DB | datasets 機構の流用: `JETUSE_DATASETS` 登録簿の `owner_sub = namespace` 行 + `JETUSE_APP` スキーマ内テーブル + デモ専用 Select AI プロファイル（**demo の tag は完全 sha1 由来** — §3.2 手順 2。8hex 衝突の排除） | **lazy** — 初回データセット投入時（既存 create_dataset を名前空間キー + registry-first で使う） |
| 会話 | `conversations.demo_id = <demo_id>` 行 | **lazy** — デモスコープ chat の初回会話作成時（§4.2） |

- **POST /api/demos は INSERT のみ**（即 `ready`）。eager にはいかなる外部リソースも作らない。
  根拠（定量は比較ドキュメント）: vector store はテナンシ上限 **10 個**（SP1-03 で LimitExceeded 実測）
  かつ作成+DP伝播に 40〜60 秒。eager だと「RAG を使わないデモ」でも枠と時間を浪費する。
  物理スキーマは実行時に ADMIN 権限（CREATE USER）が要り、アプリ資格情報の昇格になるため不採用。
- specs/17 §5 の文言「DB → デモごとに別スキーマ `demo_<id>`」は本節の**論理名前空間**として実装する
  （specs/17 の「サンプルスキーマ実体化の仕組みを流用」の実体が datasets 機構であることによる読み替え）。
- **箱あたりの上限**（codex review-5 M006 — 同期削除の所要時間を構成的に有界化する）:
  デモ箱の RAG ファイル数と dataset 数に上限を設ける（設定値。既定の目安: files 20 / datasets 10。
  超過は 422）。**走査対象の有界化**（codex review-7 M002 — 箱の上限だけではプロジェクト全体
  一覧の走査時間を抑えられない）: CP vector store 一覧は**テナンシ上限（≤10）で自然に有界**。
  DP Files の総数は**アプリ全体の上限（設定値）**を設け、**予約 ledger** で check-then-create
  競合とクラッシュ回収を閉じる（codex review-8 M005 / review-14 M005 / review-15 M001 —
  単純カウンタは「予約後・外部作成前」のクラッシュで枠が漏れ、READ COMMITTED では
  同時 Tx の未 commit 予約が見えず残り 1 枠を複数が取れる）:
  **予約の直列化 = 専用 quota 行の `SELECT FOR UPDATE`** を予約 Tx の先頭で取り、
  ledger の件数検査 + `state='pending'` 行 INSERT を同一 Tx で commit
  （超過は **422 に統一**〔codex review-11 N001〕）。ledger 列 =
  `id / owner_key / filename / external_file_id（作成後に設定・それまで NULL）/
  state('pending'|'confirmed') / created_at / updated_at`。
  **外部作成後も `pending` のまま `external_file_id` だけを記録**し、`confirmed` への更新は
  **`rag_files` INSERT と同一 DB トランザクションのみ**（一意 — codex review-18 B001。
  各更新直後の停止からの回収テスト付き）→ 行削除/失敗で ledger 行 DELETE（解放）。
  **照合の一意化 = 単一のキー導出関数を正本にする**（codex review-16 B001 / review-17 M002 /
  review-18 B003 — filename 接頭辞では同名 upload と対応が取れず、raw owner_key を埋めると
  バイト長を溢れ、箇所ごとの規則差は「作成側と削除側で見つからない」事故になる）:
  `file_key(owner_key, reservation_id, ext) = "<sha1(owner_key) 40hex>/<reservation_id>.<ext>"`
  （ext = 既存検証済みの正規化拡張子 — codex review-18 M001: 拡張子を失うと Files/vector store の
  形式判定が壊れる。pdf/md/txt の実 GenAI 取込み検証付き）、原本 object 名 =
  `rag/<sha1(owner_key)>/<reservation_id>.<ext>`。**upload・起動時 reconcile・個別 DELETE・
  demo DELETE・E2E fixture の全てがこの導出関数を共用**する。
  **Select AI 索引・citation との突合**（SP2-00 residual M003 の解決）: `rag_files.id =
  reservation_id` とする（単一 ID で ledger・DB 行・外部名・`$VECTAB` を突合）。
  `$VECTAB` の `attributes.object_name` と narrate の Sources 行は、新形式
  `<reservation_id>.<ext>` は basename の stem を file_id に、旧形式
  `<file_id>_<filename>` は uuid 接頭辞を file_id に解決する（新旧両対応の単一パーサ。
  既存 user 資産の旧名は変えない — main 互換）。（元のファイル名は外部名に含めない —
  表示名は ledger/rag_files の列から解決: 既存 `resolve_citation_filenames` の流儀。
  **表示名列は CHAR セマンティクス** — codex review-18 M003: 既存 rag_files.filename は
  BYTE のため日本語 400 文字が ORA-12899 になる。単文 migration `024` =
  `ALTER TABLE rag_files MODIFY (filename VARCHAR2(400 CHAR))`、ledger も
  `filename VARCHAR2(400 CHAR)`。API は 400 文字超を 422。最大長直前/直後 + 日本語名の
  境界テスト）。
  **ledger の状態遷移**（codex review-17 B001 — `confirmed` を File 作成時に立てると
  「confirmed だが未登録」の孤児が回収不能）: `pending → confirmed` の更新は
  **`rag_files` INSERT と同一 DB トランザクション**で確定する（confirmed = DB 登録済みの意味）。
  **ledger は locator も write-ahead 保持**（codex review-18 M004 — user 経路は
  demo_backend_targets を持たないため、region/project 変更後に旧 File を構成できない）:
  ledger DDL に `locator CLOB NOT NULL CONSTRAINT ck_rag_ledger_loc CHECK (locator IS JSON)`
  を加え、予約時に秘密値を除く locator（region / compartment / project / OS namespace+bucket）を
  記録。reconcile・削除はこの locator でクライアントを構成する（user upload 後の
  project/region 変更 + 各クラッシュ点からの回収テスト）。
  **起動時 reconcile**: 一定時間を過ぎた `pending` 行を reservation_id で exact 照合し、
  実 File / 原本が**あれば削除して予約を解放**（未登録 File を採用・再開はしない —
  クライアントの再 upload に任せるのが最も単純で冪等）。`confirmed` 行は
  **回復マトリクス**で扱う（codex review-18 M005 — 単なる「解放」では孤児/幽霊が残る）:
  (rag_files 行あり・実 File あり)=正常／(行あり・File なし)=幽霊 → 原本・索引・DB 行を
  個別削除手順（§3.2 の外部先行順）で整合回収／(行なし・File あり)=File・原本を削除して
  ledger 解放／(行なし・File なし)=ledger 解放のみ。**一覧/取得 API の一時エラーは
  「不存在」と解釈せず 503 で fail-closed**（後で再 reconcile）。4 組合せ + 一時障害のテスト。
  **ledger に無い実 File（未管理 File）を検出したら upload 経路を fail-closed（503）**にして
  人間対応（codex review-17 M003 — 未管理分を勝手に消しも数えもしない。初回導入は §3.2 の
  クリーンアップで空にしてから）。
  **完全 DDL**（`_ensure_meta` 同様の冪等 DDL + 辞書検証。各 DDL 直後停止からの再実行テスト付き。
  初期行の作成競合は ORA-00001 成功扱い — codex review-16 M001。**SP2-02 反映**: 本文の
  write-ahead 契約どおり `locator` 列を含める〔SP2-00 residual M001〕・`filename` は
  CHAR セマンティクス 400〔M002 — rag_files 側 migration 024 と対〕・`ext` 列を持つ
  〔正規化拡張子。reconcile / demo DELETE が外部名 `<rid>.<ext>` と原本 object 名を
  ledger 単独で exact に再導出するため — 一覧照合に依存しない〕）:
  `rag_file_ledger (id VARCHAR2(36) PRIMARY KEY, owner_key VARCHAR2(255) NOT NULL,
  filename VARCHAR2(400 CHAR) NOT NULL, ext VARCHAR2(10) NOT NULL,
  external_file_id VARCHAR2(128),
  state VARCHAR2(10) NOT NULL CONSTRAINT ck_rag_ledger_state CHECK (state IN ('pending','confirmed')),
  locator CLOB NOT NULL CONSTRAINT ck_rag_ledger_loc CHECK (locator IS JSON),
  created_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL, updated_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT uq_rag_ledger_ext UNIQUE (external_file_id))` + `(state, created_at)` 索引 +
  `(owner_key)` 索引、
  quota アンカー `rag_file_quota (id NUMBER PRIMARY KEY CONSTRAINT ck_rag_quota_one CHECK (id = 1))`
  の単一行（予約 Tx が FOR UPDATE で直列化に使う）。
  テスト: **残り 1 枠への user/demo 同時 upload**・**異なるユーザーの同名 upload**・初期化競合・
  予約直後/Files 作成直後/DB 登録直後の各停止と再起動後再 upload の反復で
  **孤児と quota 使用数が増えない**こと。**カウンタは Files API を呼ぶ全アプリ経路（user 単位
  `/api/rag/files` を含む）に適用する**（codex review-10 M002 — demo 経路だけでは全件一覧が
  無制限に伸び、DELETE の走査有界性が成立しない）。Public/main 互換は上限の既定値で守る:
  **既定 = 無制限（None・挙動不変）**、Internal 配備で有効値（目安 2000）を設定する。
  user/demo の同時 upload を含む上限テストを置く。
  **起動時に実件数（files.list の全走査）と ledger を突合**する（SP2-00 residual M006 の
  解決 — 「カウンタ補正」はしない。ledger が唯一の正: 期限切れ pending は exact 回収して
  解放、confirmed は回復マトリクスで整合回収、**ledger に無い実 File（未管理）は補正でなく
  upload 経路の fail-closed（503）**で人間対応へ。勝手に消しも数えもしない）。
  Object Storage 原本の扱い（codex review-9 M001 / review-16 M002 で確定）:
  put の順序は従来（登録前）のままとし、**未登録原本の回収は ledger が担う**
  （object 名に reservation_id を含めるため、pending 解放時に原本も exact に特定して削除 —
  put 直後クラッシュの反復でも prefix は有界）。
  **Select AI RAG が有効な構成では原本は「バックアップ」ではなく vector index の唯一の
  データ源**のため、put は**必須のリトライ可能ステップ**とする: put 失敗時は upload を
  成功にせず（外部 File・原本・ledger を収束削除して失敗応答 → クライアント再試行）、
  「登録済みなのに Select AI で永久に検索不能」な状態を作らない。
  put 失敗 → 再試行で Select AI 検索・citation まで回復する障害注入テストを含める。
  **受け入れ閾値の定義**（codex review-6 M007）: SP2-02 で配備経路の実効タイムアウト
  （クライアント/LB のうち最小）を実測し、**その 1/2 を DELETE 完了の受け入れ閾値**として
  検証レポートに記録する。上限フル状態 + 全体一覧走査込みの削除実測が閾値内であること。
  閾値を超える実測が出た時点で §3.3 の非同期化（202 + 完了確認 API）へ移行する
  （それが発動トリガー）。user 単位ルートには適用しない。

### 3.2 削除の後始末（SP2-02 の本体）

`DELETE /api/demos/{demo_id}` は**同期**（リクエスト内。§3.3）で次の順に後始末する。
**各ステップは冪等**（NotFound / ORA-00942 は成功扱い）で、**途中失敗しても再 DELETE で収束**する:

1. §3.2.1 の**排他リースを取得**してから `deleting` へ遷移して commit する。
   リースは **commit を跨いで保持できる方式**（§3.2.1 — DBMS_LOCK 第一案。
   `SELECT FOR UPDATE` 単独では遷移 commit で失効する — codex review-5 B001）で、
   **手順 5 の demos 行削除 commit まで保持し続ける**（後始末全体を占有。
   フォールバック方式での並行 DELETE の扱いは §3.2.1）。以後 `require_demo` と
   mutation のリース内再確認が 404 を返し、新規・in-flight の両方を締め出す。
2. **DB 箱**: `JETUSE_DATASETS` の namespace 行（**exact な owner_sub 一致**）を列挙して
   各表を `DROP TABLE ... PURGE`（ORA-00942 は成功扱い）→ 登録簿行 DELETE →
   データセット用 Select AI プロファイル DROP（不存在は無視）。
   - **登録簿を削除根拠にできる前提として、dataset 作成を registry-first に改める**（SP2-02。
     codex review-4 B002）: 「登録簿 INSERT（exact 表名・exact owner・**state='creating'**）を
     **commit してから** CREATE TABLE → VPD → GRANT → **state='ready' に更新**」の順にする。
     **一覧・プロファイル再構築は state='ready' の行だけを参照**し、途中クラッシュの
     `creating` 行が幽霊データセット表示や「存在しない表を object_list に含めて dbchat を壊す」
     事故にならないようにする（codex review-5 M001）。`creating` のまま一定時間を過ぎた行は
     次のデータセット操作時（および demo DELETE 時）に回収する（表があれば DROP → 行 DELETE。
     冪等）。**state 列の導入手順**（codex review-6 M001 / review-7 M004 — JETUSE_DATASETS は
     migration でなく `_ensure_meta` が実行時作成する表）。`_ensure_meta` に次のステップを
     順に追加する — **冪等性は「ORA コード無視」でなく辞書検証で担保**（codex review-11 M002:
     ORA-01430 の一律無視は形違い列を正常と誤認し、無名 CHECK の再 ALTER は制約を増殖させる）:
     各ステップの前に USER_TAB_COLUMNS / USER_CONSTRAINTS で**現状を検証し、不足分だけを適用**、
     期待と異なる形（型・長さ・DEFAULT・NULL 可否・制約条件）は停止して人間対応。
     (1) `state VARCHAR2(10)` 列（NULL 許容）→ (2) `UPDATE ... SET state='ready'
     WHERE state IS NULL`（既存行 backfill）→ (3) `DEFAULT 'ready'`（**根拠**: state を
     指定しない旧 writer は「CREATE TABLE 成功後に登録」の順で、その行は登録時点で実体が
     あるため ready が正しい。新実装は registry-first で必ず `'creating'` を明示 INSERT —
     DEFAULT に依存しない）→ (4) backfill 完了後に `NOT NULL` + **正規名付き**
     `CONSTRAINT ck_jetuse_datasets_state CHECK (state IN ('creating','ready'))`。
     参照側は `state='ready'` の等値判定（NULL を ready 扱いにしない — (2) 完了が前提）。
     単一インスタンス構成のため旧・新 writer の混在は起動を跨がない。テスト: 各ステップ間
     クラッシュからの再適用・形違いでの停止・複数回の通常呼び出しでスキーマが増殖しないこと。
     **未登録の表は構造的に存在しない**。短縮 hash 接頭辞（sha1[:8] = 32bit）の
     USER_TABLES 走査を削除根拠にしない（tag 衝突で他人の表まで DROP しうる +
     LIKE の `_` ワイルドカード事故）。テスト: registry commit 直後 / CREATE 直後 /
     ADD_POLICY 失敗 の各クラッシュ相当から、一覧非表示・profile 非包含・回収の収束を検証。
   - **既存 `delete_dataset` はそのまま流用しない**（codex review-3 M001: 現実装は登録簿行を
     先に消し、DROP の全例外を無視する）。SP2-02 で「DROP 先行・ORA-00942 のみ成功扱い・
     他の失敗は登録簿行を残して 503」に改修してから使う。
   - **demo 名前空間の datasets 命名（プロファイル・表接頭辞）も完全 sha1 由来へ改める**
     （SP2-02。codex review-6 B002: `JETUSE_DS_<sha1[:8]>` の衝突は「共有プロファイルの
     object_list を通じて相手の表名・列メタデータが NL2SQL 生成に混入する」情報漏えいであり、
     可用性の問題に留まらない。VPD は実行時の行を隠すだけで生成時のメタデータ共有は防げない。
     完全ハッシュなら衝突は暗号学的に無視可能）。既存 user 資産の 8hex 名は変えない
     （main 互換 — user 側の衝突リスクは main バックポート課題の residual に含める）。
     テスト: 8hex を強制一致させた 2 owner で NL2SQL 出力に相手の表・列が現れない・
     削除が波及しない。
3. **RAG 箱**（demo の RAG 経路が書き込みうる**全バックエンド** — codex review-1 B004）:
   - a. `rag_files` の namespace 行を列挙 → DP `vector_stores.files.delete` / `files.delete`
     （NotFound は成功扱い）→ 行 DELETE → **対応する `rag_file_ledger` 行を external_file_id /
     reservation_id で冪等に DELETE（枠の解放 — codex review-16 B002: 箱の削除でも quota を
     返さないと作成・削除の反復で上限が永久 422 になる）**。E2E 事後条件に「demo DELETE 直後に
     再起動なしで再 upload できる（枠が戻る）」「作成・削除の反復後に ledger 件数と
     files.list 実件数が一致する」を含める。
   - b. vector store 本体: `rag_stores` 行の ID に加え、**CP `vector_stores` 一覧から
     `metadata.owner == sha1(owner キー) 40hex`（固定長の完全ハッシュ — codex review-17 M004:
     raw キーは user 側で 255 バイトまであり metadata の 64 文字枠に入らない。完全ハッシュなら
     全経路で一意・64 文字内）で照合したものを列挙**して削除（NotFound = 成功。
     登録前クラッシュで未登録のまま残った store も回収 — codex review-3 B003 / review-4 B002。
     作成側も同じ導出値を metadata に保存する）。
     **一覧はページネーションを完走してから選別する**（先頭ページのみは不可 — codex review-5
     M004。原本 prefix と同じ要求）。**store 削除の失敗は中断（503）** — テナンシ上限 10 の
     枠漏れを防ぐため best-effort にしない。
     **`rag_stores` 行と `demo_backend_targets` 台帳行の DELETE は 3f まで全て成功した後**に
     行う（codex review-11 B002 / review-13 B001 — 後段（select_ai / opensearch / 原本）の
     503 やクラッシュ後の再 DELETE が旧構成の locator を参照できるよう、RAG 箱の掃除完了まで
     保持する）。
   - c. **OCI Files の未登録孤児と ledger の全行解放**: 外部 filename は §3.1 の
     `file_key(owner_key, reservation_id, ext)`（= `<sha1(owner_key)>/<reservation_id>.<ext>` —
     作成・削除・fixture すべて同じ導出関数。codex review-18 B003）。後始末は
     (i) `rag_file_ledger` の owner_key = namespace の**全行（pending/confirmed）を列挙**し、
     reservation_id で File・原本を削除 or 不存在確認してから ledger 行を解放
     （codex review-18 B002 — 予約直後・put 直後・File 作成直後に停止した pending 行は
     rag_files に対応が無く、これを解放しないと quota 枠が恒久に漏れる）、
     (ii) DP `files.list` を**ページネーション完走**で列挙し `<sha1(namespace)>/` 接頭辞一致を
     削除（ledger にも無い孤児の保険。先頭ページのみは不可 — codex review-5 M004）。
     **事後条件: 当該 owner の ledger 行がゼロ**（E2E で確認）。
   - d. **Select AI RAG**（rag_select_ai）: namespace の profile と vector index を DROP
     （不存在は無視。`$VECTAB` は index とともに消える）。
     **前提改修（SP2-02）**: demo 名前空間の profile/index 名は**完全 sha1（切詰めなし）**から
     導出する（codex review-5 B002: 現行 sha1[:8] = 32bit は衝突時に 2 owner が同一 index を
     共有 = 相手の文書を検索でき、削除も波及する。完全ハッシュなら衝突は暗号学的に無視可能で、
     認可・削除根拠が exact owner の決定的関数になる）。既存 user 資産の 8hex 名は変えない
     （main 互換 — user 側の衝突リスクは main バックポート課題として residual に含める）。
     テスト: demo 2 namespace で profile/index/location が完全に分離・削除が波及しない・
     名前導出が完全ハッシュであること。
   - e. **OpenSearch**（rag_opensearch）: namespace の index を index ごと DELETE
     （404 は成功扱い）。**demo の index 名も完全 sha1 由来へ改める**（codex review-7
     M001: 既存の sha1[:16] は衝突時に index を共有 = 検索混入・削除波及。datasets /
     select_ai と同じ理由で切詰めハッシュを認可・削除根拠にしない。既存 user 資産の
     16hex 名は変えない — main 互換・residual）。
   - f. **Object Storage 原本**: prefix `rag/<sha1(namespace)>/`（§3.1 の導出規則と同一 —
     codex review-18 B003）のオブジェクトを**ページネーション付きで全列挙して削除**する。NotFound 以外の失敗は**中断（503）** — best-effort にしない
     （codex review-2 B003: 行削除後は再試行で辿れなくなるため、原本の残骸も 503 → 再 DELETE の
     収束契約に含める）。**バケットの versioning は配備 preflight と DELETE 実行時の両方で
     「Disabled」を必須確認**する（codex review-17 M006 — versioning 有効だと通常一覧が空でも
     旧 version と delete marker に原本が残り「残骸なし」が偽陽性になる。**`Suspended` も不可** —
     SP2-00 residual M004 の解決: OCI OS の Suspended は新規 version を作らないだけで
     **既存 version は残る**ため、`Disabled`（一度も有効化されていない）以外は該当経路を 503 とし、
     全 version 削除対応を人間へ差し戻す）。
   - **スキップ判定の正は台帳**（codex review-14 B001 — e/f を「現在 enabled / バケット設定あり」で
     判定しない）: `demo_backend_targets` に当該 kind の行があれば、現在の設定が無い/一致しなくても
     **必ずその locator で削除を試み、構成できなければ 503**（台帳と demo 行を保持）。
     スキップしてよいのは**台帳にも行が無い**（そのバックエンドを一度も使っていない）場合だけ。
   （**削除対象の名前は各バックエンドの既存命名関数から取得する** — codex review-2 M001:
   命名規約はバックエンドごとに異なり（例: opensearch は sha1 先頭16桁小文字、select_ai は
   先頭8桁大文字）、後始末側で再実装すると乖離 = 残骸の温床になる。SP2-02 で各モジュールに
   `delete_owner(namespace)` 相当の公開関数を設け、後始末はそれだけを呼ぶ）
   - **既存 demo 資源の移行（SP2-02 配備前・一回限り）**（codex review-10 B001 — SP1-03 時点の
     demo RAG は旧命名〔select_ai 8hex / opensearch 16hex / 接頭辞なし OCI File〕で作られており、
     新命名関数の後始末では拾えない）: 配備前移行スクリプトで、**demo に連結して特定できる資源**
     （demos 行・owner_sub が demo namespace の rag_stores/rag_files/JETUSE_DATASETS 行と
     その参照先・demo namespace から旧命名関数で導出される profile/index/opensearch index・
     登録行が指す OCI Files・namespace prefix の原本）を列挙し**人間承認のうえ削除**する
     （Internal は未リリースで実ユーザーの demo は存在しない — SP1 の demo は E2E シードのみ）。
     **demo に連結して特定できない資源は自動削除しない**（codex review-11 M001 — 隔離リストに
     載せて人間が個別選択。「資源ゼロ」を口実に無関係な user 資産まで消さない）。
     **旧命名（短縮 hash）から導出した削除対象名が user 側（user キーからの導出）とも一致する
     場合は削除対象から外して隔離**する（hash 衝突の巻き添え防止 — codex review-12 B002）。
     実行順序は §3.2.1 の「preflight 分類 → 人間承認 → クリーンアップ → 再検証」に従う。
     移行 E2E: 旧形式 demo 資源 fixture + **無関係な user store/file/profile（強制 hash 衝突
     ケース含む）を併置**し、demo 資源だけが消え user 資産が保持されることを実環境で確認する。
   - **バックエンド構成ドリフトへの耐性**（codex review-10 B002 / review-11 B002 /
     review-12 B003 / review-13 B001）: 箱が書き込むバックエンドの **write-ahead 台帳は
     独立表** `demo_backend_targets` とする。単文 migration `022`（完全 DDL —
     codex review-14 M001。ランナーの事後条件検証・fault-injection の対象は 017〜024）:
     `CREATE TABLE demo_backend_targets (id VARCHAR2(36) PRIMARY KEY,
     namespace VARCHAR2(255) NOT NULL, kind VARCHAR2(20) NOT NULL
     CONSTRAINT ck_dbt_kind CHECK (kind IN ('vector_store','files','select_ai','opensearch','objectstorage')),
     locator CLOB NOT NULL CONSTRAINT ck_dbt_locator CHECK (locator IS JSON),
     locator_hash VARCHAR2(64) NOT NULL,
     created_at TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
     CONSTRAINT uq_dbt UNIQUE (namespace, kind, locator_hash))` +
     `023_dbt_idx.sql` = `CREATE INDEX idx_dbt_ns ON demo_backend_targets(namespace)`。
     **書き込みは正規化 locator のハッシュに対する冪等 upsert**（ORA-00001 は成功扱い —
     codex review-15 B002: 素の追記型は upload/削除の反復で無制限に伸び、同期 DELETE の
     走査有界性を壊す。distinct な target 数〔構成変更の回数〕でのみ増える）。
     **locator_hash の正規化形**（SP2-00 residual N001 の解決）: 空値（None/空文字）の
     キーを除外し、文字列値は末尾スラッシュを除去（endpoint 表記ゆれの吸収）、キーを
     辞書順に整列したコンパクト区切りの JSON（ensure_ascii=False）に対する **sha256 hex**。
     大文字小文字は変換しない（OCID・bucket 名は大小が意味を持つ）。
     長期間の upload/delete 反復後でも DELETE が閾値内に完走する試験を含める —
     `rag_stores` 列では実現できない（`vector_store_id` が NOT NULL のため **store ID が
     得られる前に行を作れず**、store 作成直後クラッシュ + region/project 変更で locator を失う）。
     記録は**秘密値を除く完全 locator**（種別 + region / compartment OCID / GenAI project OCID /
     Object Storage namespace+bucket / OpenSearch endpoint）を**外部書き込みの前に** INSERT・
     commit し（store は ID 確定前に locator を記録 → 作成 → rag_stores 登録）、
     **複数の過去 target を保持**できる追記型とする。削除は台帳の全 locator でクライアントを
     構成して行い（「現在の設定で削除して NotFound=成功」では旧 target の資源を見逃す）、
     台帳行は RAG 箱の掃除が全て成功した後に DELETE する。
     初回 store 作成直後クラッシュ + region/project 変更からの回収をテスト。
     **記録があるのに接続設定が無い/一致しない場合はスキップせず 503**（demo 行を保持 —
     設定復旧後の再 DELETE で収束）。設定無効化・bucket 変更・**project/compartment 変更**後の
     DELETE、および各外部書き込み直後クラッシュからの再 DELETE 収束をテスト。
   - **ensure_store の孤児採用（前提改修）**（codex review-15 M002 — 作成後・登録前クラッシュの
     未登録 store は demo DELETE でしか回収されず、upload 再試行が新 store を作り続けると
     テナンシ上限 10 を demo 削除なしに使い切れる）: `ensure_store` は新規作成の**前に**
     CP 一覧（ページネーション完走）から `metadata.owner == sha1(owner キー) 40hex` の一致を
     検索し、見つかれば**採用して登録簿へ収束**させる（複数あれば **store 状態を考慮して
     最古の usable〔status=completed〕を正本**とし、残り〔failed / 中途状態を含む〕を削除 —
     SP2-00 residual M005 の解決。usable が無ければ全て削除して新規作成に進む。
     照合は固定長完全ハッシュ — 手順 3b と同じ導出。64 文字境界超の user sub でも成立）。
     作成直後クラッシュ → demo を削除せず再 upload が成功する E2E（長い user sub のケース含む）。
   - **個別ファイル削除の外部先行化（前提改修）**（codex review-11 B003 — 既存 `delete_file` は
     DB 行を先に消し外部削除の失敗を無視するため、demo の `DELETE .../rag/files/{id}` を反復すると
     箱上限とカウンタを解放しながら OCI File・原本を無制限に残せる）: `delete_dataset` と同型に
     「**外部削除（NotFound は成功扱い）成功 → 行 DELETE → 予約カウンタ解放**」の順へ改修し、
     NotFound 以外の失敗は 503 で行とカウンタを保持する。**Select AI の索引（`$VECTAB`）への
     反映も削除の一部**（codex review-12 M003 — 原本を消しても索引は更新周期まで当該文書で
     回答し得る）: 原本削除後に**索引の再同期を同期実行**して `$VECTAB` からの不存在を
     確認してから行を消す（**同期一択** — codex review-14 M004: tombstone 方式は応答・一覧・
     カウンタ・検索遮断の状態機械を別途要求する割に、$VECTAB は残存中も検索に使われ続けて
     「削除後検索で出ない」を保証できない。再同期が失敗/不可能なら 503 で行とカウンタを保持し
     再試行で収束）。削除後検索で当該文書が出ないことを E2E に含める。
     反復削除失敗でも実資源数が上限内に収束することをテスト。
     dev 枝での改修 — main への展開は residual（§4.3 の注と同じ扱い）。
4. **会話**: **messages を先に明示チャンクで削除**（demo 会話に属する messages を
   `ROWNUM <= 1000` 等のチャンクごとに DELETE + commit — codex review-13 B002:
   親行の CASCADE に任せると「1 会話に大量 message」だけで各試行がタイムアウト →
   全量ロールバックで収束しない）→ 会話の messages がゼロになってから conversations の
   demo_id 行を同様にチャンク削除する（codex review-12 B004。チャンク commit なら
   **タイムアウトしても進捗が残り、再 DELETE が続きから収束**する）。
   「1 会話に大量 message」fixture の実 ADB 障害注入・再開テストを含める。
   **外部リソースは無い** — §4.2 の決定により demo 会話は OCI Conversation を作らない
   （DB リプレイの stateless 経路。外部会話の item 削除・クラッシュ孤児・LTM 混線の
   問題クラスごと排除 — codex review-4 B004）。大量会話 fixture での削除実測を E2E に含める。
   **usage_log は削除しない（明示の保持契約）**: usage_log は課金/利用量の監査記録で
   実ユーザーに紐づく（SP1-03 の「監査は人に紐づける」原則）。demo 削除後、行中の
   conversation_id は解決不能な参照になるが、これは意図した保持であり「箱のデータの残骸」
   ではない（codex review-4 M002 — 削除でなく保持を契約として定める）。
5. `demos` 行 DELETE → commit（ここでリース解放）→ `{"deleted": true}`。

- 失敗時の応答: その段階を detail に含む **503**（`status='deleting'` の行が残る）。
  クライアントは同じ DELETE を再実行すれば良い（各ステップ冪等なので必ず収束する）。
- 順序の原則: **掃除の列挙は exact な根拠に限る**（表 = registry-first の登録簿〔exact owner〕、
  store = CP 一覧の metadata exact 一致、file = 完全 namespace の filename 接頭辞、
  原本 = 完全 namespace の prefix、profile/index = 各バックエンド命名関数の決定的名前）。
  クラッシュで未登録になりうるのは store / file のみで、どちらも exact 根拠（metadata / 接頭辞）で
  回収できるため、補償削除や cleanup record を持たずに「再 DELETE で収束」が成立する。
  短縮 hash の接頭辞走査は削除根拠にしない（誤削除の温床 — codex review-4 B002）。
  DB 行はそれぞれの外部削除の**後**に消す。
- 外部リソースを作る操作のうちクラッシュで未登録残骸になりうるのは vector store と
  OCI Files のみで、どちらも上記 3b（metadata exact 一致）・3c（filename 接頭辞）の
  実在ベース回収が拾う（demo 会話は外部リソースを作らない — 手順 4）。

#### 3.2.1 lazy 生成との競合設計 = demo 単位の排他リース
（codex review-1 B003 / review-2 B002 / review-3 B001・B002・B003）

`require_demo` を通過済みの in-flight リクエストが、削除側の列挙**後**に箱の実体を作ると
残骸が残る。review-2 までの「登録 Tx の行ロック + 補償削除」案は、(1) dataset 投入の
CREATE TABLE/GRANT が **Oracle DDL の暗黙 commit** でトランザクションロックを途中解放する、
(2) `files.create` / store attach 等**登録行を持たない外部作成**が窓に残る、(3) **補償削除自体の
失敗**で収束契約が破れる — の 3 点で成立しない（review-3）。よって補償方式を捨て、
**単一の排他リース**に一本化する:

- **リースの定義**: demo 名前空間に対して外部リソース・DDL・登録行を作る**全ての操作**
  （RAG upload の全段 = 原本 put / `files.create` / store attach / `rag_files` INSERT /
  OpenSearch ingest、`ensure_store` の store 作成、dataset 投入の registry-first 登録〜
  CREATE TABLE、`ensure_profile` の profile/index 作成、demo 会話 INSERT）は、
  操作の**開始から完了まで** demo 単位の排他リースを保持して行う
  （demo 会話は OCI Conversation を作らない — §4.2 — ため外部作成はリスト前段のみ）。
  **再入契約**（codex review-10 M001 — upload が保持したまま内側の `ensure_store` が
  再取得すると自己待機で timeout する）: リースは**最外層だけが取得・解放**し、
  取得済みのリーストークン（保持セッション）を内部関数へ引数で伝播する。
  同一 demo のネスト呼び出しはトークンの保持を検証して再利用（再取得しない）。
  ネスト試験（upload → ensure_store / dataset 投入 → profile 再構築）を置く。
- **リースの実装（第一案 = DBMS_LOCK）**: **専用 DB セッション**で
  `DBMS_LOCK.REQUEST(ora_hash(demo_id) ベースのロック ID, X モード, timeout)` を取得して保持する。
  **セッションスコープのロックであり、トランザクションの commit を跨いで保持できる**
  （codex review-5 B001: `SELECT FOR UPDATE` は deleting 遷移の commit で失効するため
  リースに使えない）。**取得後の status 再確認は 2 契約に分ける**（codex review-7 B003）:
  **mutation 取得**（公開 API 既定）= 行なし/`deleting` は 404。
  **DELETE 取得**（内部専用 `allow_deleting`）= 行なしは 404、`deleting` は**受理**して
  後始末を再開する（途中失敗 503 後の再 DELETE が収束するための要。
  各後始末段階で 503 にした後、同じ DELETE が完走するテストを段階ごとに置く）。
  操作完了（DELETE では手順 5 の commit 後）に RELEASE して接続返却。セッション死 = ロック自動解放。
  **作業本体は別接続で行ってよい**（DDL の暗黙 commit はリースに影響しない）。
  ロック ID のハッシュ衝突は別デモ間の過剰直列化になるだけで正しさに影響しない。
  **単位を明示する**（codex review-4 M005 / review-5 M005）: `DBMS_LOCK.REQUEST` の
  timeout は**秒**（目安 300）、リース待ちの DB 呼び出しを跨ぐ oracledb の
  `Connection.call_timeout` は**ミリ秒**（目安 `310_000`。既定 10 秒 = `10_000` のままでは
  待てない）。リース専用セッションにのみ設定し、接続返却時に既定値へ戻す。
  境界値（WAIT 上限直前/直後）のタイムアウト試験を含める。
  **実装契約**（codex review-6 M002 / review-8 M003）: ロック ID は有効範囲内の決定値
  （例 `ORA_HASH(demo_id, 1073741823)`）。`REQUEST` は **`release_on_commit => FALSE` を明示**し
  （commit を跨ぐ保持の前提）、**例外でなく戻り値**で結果を返すため全戻り値を処理する
  （0=成功 / 4=既保持 以外 — 1 timeout・2 deadlock・3 パラメタ・5 不正ハンドル — は
  エラーとして 503/500 に正規化）。解放は finally で必ず `RELEASE` し、**`RELEASE` も
  戻り値 0 のみを成功扱い**とする。RELEASE 非 0 または例外時は**接続をプールへ返さず破棄**する
  （残留セッションロックが後続を待たせ、同一セッションの REQUEST=4 を新規取得成功と
  誤認するのを防ぐ）。RELEASE 異常戻り値と再取得のテストを含める。
  リース保持セッションは**リース専用の小プール**から取る（既存作業プール〔最大 4〕と分離 —
  リース保持中に作業接続を待ってプールが枯渇するのを防ぐ。専用プールのサイズ =
  同時 demo mutation の想定上限。上限到達時は 503）。プール上限数の同時 mutation テストを含める。
  **DBMS_LOCK の EXECUTE 付与は初回人間承認の対象**（Oracle の推奨に沿い、REQUEST/RELEASE の
  最小機能だけを晒す **cover package（JETUSE_APP 所有・definer's rights）経由**を第一案とする —
  codex review-8 M006）。
  **ADB での DBMS_LOCK（REQUEST/RELEASE）利用可否を SP2-02 冒頭で実機確認**する。
  **不可の場合は fail-closed**: demo の mutation / DELETE 経路を 503 で停止し、本節を
  ADR 起票へ差し戻して人間判断（codex review-6 B001 — FOR UPDATE は DDL の暗黙 commit で
  失効するため代替リースにならない。弱い代替で残骸リスクを黙認しない）。
  採用可否と実測を検証レポートに記録する。
  **リース API は scope を明示引数で受ける**（`acquire_demo_lease(demo_id)` を DemoContext
  経由で呼ぶ。owner キー文字列の `demo_` 接頭辞から推測しない — codex review-4 M006）。
  **キー空間の分離は owner キー導出の単射エスケープで構造保証する**（codex review-6 M005 —
  OIDC の sub は opaque であり認証段階で形式拒否しない）: user 経路の owner キーは
  単一ヘルパーで導出し、**sub が予約接頭辞（`demo_` / `sub_`）で始まる場合のみ `sub_` を
  前置**する（決定的・単射。実在の sub には no-op = 既存データと互換）。
  **長さの境界**（codex review-10 M003 — owner_sub 列は VARCHAR2(255) のため、252 文字超の
  予約接頭辞 sub は前置で溢れる）: 前置後に 255 バイトを超える場合は
  `sub_h_<sha1(sub) 40hex>` 形式（決定的・長さ有界。衝突は暗号学的に無視可能）へ切り替える。
  251〜255 文字の予約接頭辞 sub での作成・取得の境界値テストを置く。
  **互換の検証を明文化する**（codex review-7 M003 / review-8 M001 / review-9 B002）:
  **符号化導入前に一回だけ実行する Python preflight**（migration の SELECT では失敗させられず、
  実行時作成表〔JETUSE_DATASETS〕は migration 時に存在しないため）。
  順序は **「preflight（分類）→ 人間承認 → §3.2 の一括クリーンアップ → 再検証 + マーカー」**
  （codex review-12 B002 — 分類より先に破壊的クリーンアップを走らせると、既存 demo namespace と
  偶然一致した導入前 raw sub の user 資産を分類前に消しうる）:
  (1) preflight が owner キーを持つ各表（rag_stores / rag_files / JETUSE_DATASETS /
  conversations）の**予約接頭辞行を全列挙**し、demos 表との対応・作成時期等の手掛かりを添えた
  **分類リスト**を作る（conversations の owner_sub に `demo_` キーは正規に存在しない —
  demo 紐付けは demo_id 列 — ため全て要分類）。この間、該当経路は fail-closed（503）。
  (2) 人間がリストを分類・承認（削除対象 = demo 資産）。
  (3) **user 資産と分類された予約接頭辞行が存在した場合は、本仕様の範囲で移行しない**
  （codex review-13 B003 / review-14 B002・B003 — rekey は原本 copy・store re-attach・
  再 ingest・進捗台帳まで要求する重機構になる一方、この行は**現実の配備では存在し得ない**:
  Internal は未リリースで実ユーザーが居らず、Public/main は AUTH_REQUIRED=false の
  単一 `dev-user`。予約接頭辞の実 sub が本当に見つかったなら、それは想定外の環境であり
  **spec-driven の既定どおり個別 ADR に差し戻して人間が移行計画を設計する** — 本仕様が
  約束するのは「該当経路の fail-closed（503）を維持し、勝手に消しも変換もしない」ことのみ）。
  (4) クリーンアップ実行後、**残存ゼロを再検証**して導入マーカー
  （例: schema_migrations に `owner_key_v1`）を記録し、以後 preflight はスキップ
  （導入後は正規の `demo_<uuid>` / エスケープ済み `sub_...` が正当に存在するため。
  user 分類行が残る限りマーカーは記録されない = ADR 解決が先）。
  テスト: 既存 demo と同じ UUID を含む予約 sub の会話/RAG/dataset fixture が分類リストに
  載り自動削除されず、マーカーが記録されないこと・正規キー作成後の再起動が素通りすること。
  owner キーの導出は**単一ヘルパーに集約**し、永続資産（rag_stores / rag_files /
  JETUSE_DATASETS / conversations / VPD コンテキスト）すべてで同じ関数を使う
  （`user.subject` の直渡しを禁止 — §4.3 の呼び出し元契約も同ヘルパー経由）。
  demo キーは常に `demo_<uuid>`（サーバ生成 UUID）。
- **削除側**: 手順 1 で同じリースを取得し、**deleting への遷移も後始末もリースを保持したまま**
  行い、手順 5 の demos 行削除 commit 後に解放する（進行中 mutation の完了待ち +
  並行 DELETE の直列化。DBMS_LOCK はセッションロックのため遷移 commit で失効しない —
  codex review-4 M001 / review-5 B001）。後着の DELETE はリース解放後に行なしを見て 404
  （= 冪等な再実行の成功形）。途中失敗（503）時はリクエスト終了でリースが解放され、
  status='deleting' の行が残る → 再 DELETE がリースを取り直して再開する。
  **「作成済み・未登録」の中間状態はリースの下では DELETE と並行し得ない**ため、
  補償削除の契約そのものが不要になる。クラッシュで中間状態が残った場合はリースが
  自動解放され（セッション死 = ロック解放）、§3.2 の**実在ベースの回収**
  （store = metadata exact 一致 / file = 接頭辞。表は registry-first で中間状態が無い）が拾う。
- **直列化の影響**: mutation は demo 単位で直列化される。書き込み系は所有者のみ・
  会話作成/ingest は秒オーダーのため、SA 個人規模のデモで実害はない（並行が要る規模に
  なったら共有/排他の 2 モード化を検討 — その時点の実測で判断）。
- **チャットの SSE 本体はリースを跨がない**: リースは登録行の作成区間（秒オーダー）だけで
  保持し、LLM 呼び出し・SSE 配信中は保持しない（公開デモの並行チャットを直列化しない。
  メッセージ行の追記は外部リソースを作らないためリース対象外 — 手順 4 の一括削除が拾う）。
- **並行 DELETE 同士**: リースで完全に直列化される（保持範囲 = 後始末完走まで）。
  後着は行なし（先着成功）なら 404、deleting 行（先着途中失敗）なら後始末を再実行して収束。
- user 単位ルートは**リースの対象外**（demos 行と無関係）だが、**資源キーの導出は
  user 経路も `owner_key(user.subject)` を必ず通す**（codex review-10 B004 —
  `sub='demo_<uuid>'` のユーザーが同名 demo の資源キーと衝突するのを防ぐのがこの符号化の目的。
  「変更しない」が指すのは公開パス・レスポンス契約とリース適用範囲であり、キー導出ではない）。
- **テスト**: fake 層で「mutation 進行中の DELETE が完了まで待つ」「deleting 遷移後の mutation が
  404」「**同時 2 本の DELETE**（先着が後始末・後着が 404/再開）」「クラッシュ相当の未登録残骸
  （store・file）→ 再 DELETE の実在ベース回収」「tag を強制衝突させた 2 owner で削除・VPD が
  互いに波及しない」。実 ADB E2E で (a) dataset 投入（DDL）前後に DELETE を差し込む並行
  シナリオ（fake では DDL 暗黙 commit を再現できない — review-3 B001）、
  (b) **15 秒超のリース保持中の DELETE 待機**（call_timeout 設定の実機確認 — review-4 M005）
  を各 1 本以上実施。

### 3.3 同期/非同期（選択肢と決定）

| 案 | 内容 | 評価 |
|---|---|---|
| **同期（採用）** | DELETE リクエスト内で §3.2 を完走 | 削除の実測オーダーは秒（store delete 1 API call + 表 DROP 数個）。追加部品ゼロ。失敗時も「503 → 再 DELETE」で契約が単純 |
| 非同期（worker） | ジョブキュー/常駐 worker で後始末 | 現行構成に worker が無く導入コストが過大。デモ数規模（SA 個人単位）に対して過剰 |
| 非同期（BackgroundTasks） | FastAPI BackgroundTasks | 将来候補として留保。冪等後始末はそのまま流用できるが、**API 契約の変更が必要**（202 応答 + 完了確認手段。`deleting` は require_demo が 404 に秘匿するためポーリングには使えない — codex review-3 N002） |

**採用 = 同期**。切替の判定は **§3.1 の受け入れ閾値（配備経路の実効タイムアウト実測の 1/2）に
一本化**する（codex review-11 N002 — 固定 30 秒と実測閾値の二重基準にしない）: 閾値超過の
実測が出た時点で BackgroundTasks 化（202 + 完了確認 API）へ移行する。
生成側はそもそも eager 処理が無いため同期/非同期の論点自体が消える（§3.1 の lazy 決定の含み益）。

## 4. DemoContext 解決先 + dbchat デモスコープ化（SP2-03）

### 4.1 DemoContext

- 解決キーは **`namespace` 一本を維持**する（箱が論理名前空間なので、DB・RAG・会話とも
  同じキーで解決できる。`db_schema` 等の別フィールドは追加しない）。
- 追加は `status` のみ（§2.3 — deleting ゲートに使用）。

### 4.2 会話のデモ紐付け

- SP1-03 の暫定拒否（demo chat への conversation_id 持ち込み 422）を解除する。
- **作成の契機**（既存 user フローと同型 — クライアントが先に会話を作ってから chat に渡す）:
  `POST /api/demos/{id}/conversations`（`require_demo` — 公開デモで chat を実行できる者は
  会話も持てる。行は `owner_sub = owner_key(user.subject)`・`demo_id = ctx.demo_id` で作成し、
  レスポンスは既存 `POST /api/conversations` と同形で `id` を返す）。
  demo chat の SSE 自体は会話を自動作成しない（既存 user 単位 chat と同じ契約）。
- `POST /api/demos/{id}/chat` で `conversation_id` 指定時:
  `conversations.owner_sub = owner_key(user.subject) AND demo_id = ctx.demo_id` を検証
  （不一致・不存在は 404）。
- **キー列の区別**（codex review-8 M002 — 作成・照合・user 経路で条件を一致させる）:
  `conversations.owner_sub` を含む**資源キー列**（rag_stores / rag_files / JETUSE_DATASETS /
  conversations の owner_sub）は常に **§3.2.1 の導出ヘルパー `owner_key(sub)` 経由**。
  `demos.owner_sub` は**識別列**（require_demo が user.subject と直接比較する raw sub）で、
  ヘルパーを通さない。予約接頭辞（`demo_`/`sub_`）で始まる sub について user/demo 会話の
  作成・継続・両方向持ち込みの回帰テストを置く。
- **demo 会話は OCI Conversation（サーバ側会話状態）を作らない**（codex review-4 B004 対応の決定）:
  継続の契約 = **クライアントが `ChatRequest.messages` に全履歴を再送する**（既存 SPA の
  user 単位 chat と同じ流儀 — LLM への入力はリクエストの messages であり、サーバは履歴を
  合成しない。codex review-5 M002: 「DB リプレイが実装済み」という前提は誤りのため、
  サーバ側合成は採用しない）。conversation_id の役割は**履歴の保存と箱への紐付けのみ**。
  E2E: 第 2 ターンで全履歴 + 新規発話を送り、第 1 ターンの内容を踏まえた応答になること。
  これにより (1) 外部会話の item 削除・親削除の順序契約、(2) 作成クラッシュの孤児
  （Conversations に一覧 API が無く回収不能）、(3) **LTM（長期記憶）の
  `memory_subject_id=user.subject` がデモ間で記憶を共有してしまう混線** — の 3 問題を
  構造的に持たない。サーバ側会話状態・LTM が必要になったら SP3 で
  `(demo_id, user.subject)` 合成 subject を設計してから有効化する（それまで demo chat の
  LTM は無効）。テスト: demo chat が OCI Conversation 作成 API を呼ばないこと・
  デモ間で記憶が混ざらないこと（fake で検証）。
- **user 単位の会話 API は全 verb で `demo_id IS NULL` を強制**する（一覧だけでなく
  GET / DELETE / title 生成、および `/api/chat/stream` の conversation_id 継続・履歴保存も —
  demo 会話を user 経路へ持ち込めない・逆も 404。箱の混線防止）。実装は
  `conv_repo.get_conversation` 系の owner 条件に demo_id 条件を加える（既存データは全行
  demo_id NULL のため Public 挙動は不変 = 回帰なしをテストで担保）。
- デモ会話の一覧・履歴取得・個別削除 API はデモ SPA の要件が確定する SP3 で追加する
  （SP2 は「作成 → conversation_id で chat 継続」の往復まで。デモ会話の行は
  デモ削除の後始末 §3.2 手順 4 で消える）。

### 4.3 dbchat 縦切り（SP1-03 の rag と同型: 既存ハンドラの共有内部関数化）

デモの DB 箱には datasets しか存在しないため、**デモスコープ dbchat = datasets ターゲット固定**:

| Method/Path | 認可 | 実体（owner キー = ctx.namespace） |
|---|---|---|
| `POST /api/demos/{id}/dbchat/nl2sql` | `require_demo` | `datasets.ensure_profile(namespace, **`config.dbchat.model`**)`（正規キー契約は §2.2 — 省略時は既定モデル・PATCH 時 allowlist 検証）→ `generate_sql_select_ai`（SSE・keepalive は既存と同一）。**リクエストの model 入力は demo 経路では無視**（codex review-7 M005: 非所有者がモデルを交互指定すると共有プロファイルの再構築・warmup を繰り返し他利用者へ影響する — 読み取り認可の裏で共有箱を書き換えさせない）。モデル変更 = owner の PATCH（config 経由）のみ。テスト: config なし・未知モデル 422・モデル変更後の profile 再構築 |
| `POST /api/demos/{id}/dbchat/execute` | `require_demo` | `execute_readonly`（JETUSE_QUERY・読取専用）+ **越境ガード**（下記） |
| `GET /api/demos/{id}/dbchat/schema` | `require_demo` | 箱の datasets（登録簿 `owner_sub=namespace`）から表・列を返す共有関数化 |
| `GET /api/demos/{id}/db/datasets` | `require_demo` | `datasets.list_datasets(namespace)` |
| `POST /api/demos/{id}/db/datasets` / `POST .../datasets/generate` | `require_demo_owner` | `create_dataset / generate_dataset(namespace, ...)` |
| `GET /api/demos/{id}/db/datasets/{ds_id}/preview` | `require_demo` | `preview(namespace, ds_id)` |
| `DELETE /api/demos/{id}/db/datasets/{ds_id}` | `require_demo_owner` | `delete_dataset(namespace, ds_id)` |

- **datasets 表の越境防止は DB 側の境界（VPD）を正とする**（codex review-2 B005 / review-3 B004）:
  JETUSE_QUERY は全ユーザー・全デモの `JETUSE_DS_*` 表へ個表 GRANT を持ち、SQL 文字列の
  識別子走査は動的 SQL（例: PUBLIC 実行可能な `DBMS_XMLGEN` に文字列連結で表名を渡す）で
  迂回できるため、**アプリ層の走査は信頼境界にならない**。多層で守る:
  - **層1（境界 = VPD / DBMS_RLS）**: datasets 表に行レベルポリシーを付与する。
    照合は **exact 一致のみ**（codex review-4 B003 — 短縮 tag〔32bit〕の一致を境界にしない）:
    セッションコンテキストには **owner キーの完全文字列**を設定し、ポリシー関数は
    `JETUSE_DATASETS` 登録簿（表名 → exact owner_sub の対応 — registry-first で必ず存在）を
    引いて一致時のみ全行、**対応行なし・コンテキスト未設定は必ず 0 行**（fail-closed）。
    **動的 SQL・関数経由でもポリシーは適用される**ため迂回クラスごと閉じる。
    JETUSE_APP（所有スキーマ）には適用しない（アプリ内部経路は従来どおり）。
    **付与の順序と失敗契約**（codex review-4 M003）: 新規表は
    「registry INSERT（commit）→ CREATE TABLE → **ADD_POLICY 成功 → GRANT**」の順とし、
    ADD_POLICY 失敗時は GRANT せず表を DROP して失敗を返す（無保護表を JETUSE_QUERY から
    読める状態にしない）。既存表への遡及は「コンテキスト設定コードの配備 → ポリシー一括付与」の
    二段階（単一インスタンス構成では同一リリース内の起動 migration 順で満たす）。
    **必要な DB オブジェクトと実行主体**（codex review-5 M003 — 追加権限は「実行時ゼロ」であって
    セットアップ時はゼロではない）: アプリケーションコンテキスト（`CREATE ANY CONTEXT` が必要）・
    コンテキスト setter パッケージ（CREATE CONTEXT の USING に紐づく信頼パッケージ。
    JETUSE_APP 所有・JETUSE_QUERY へ EXECUTE 付与）・ポリシー関数・DBMS_RLS の
    ADD_POLICY 実行権。**初回セットアップ（権限付与・既存表への一括付与）は、対象 DB と
    作成/変更オブジェクトの一覧を提示して人間承認のうえ実行**する（codex review-6 M003 —
    既存リソースと DB 権限の変更は人間ゲート。jetuse-dev の loop 環境も承認証跡を
    `runs/<run-id>/e2e/APPROVAL.md` に残す）。承認後の**通常起動の bootstrap は
    「検証 + 承認済み定義の冪等再適用」に限定**する（実行時のアプリ資格情報は昇格しない）。
    **起動時の完全性検証は fail-closed**（codex review-6 B003 — 現行 bootstrap は失敗を
    ログだけにして起動継続するため、境界には使えない）。**順序: まず registry の `creating`
    残骸を reconcile（表があれば DROP → 行 DELETE — §3.2 手順 2 の回収と同じ）してから
    完全性を判定する**（codex review-12 M001 — CREATE 直後クラッシュの VPD なし表を
    完全性違反として 503 にすると、回収を行う「次の dataset 操作」自体が到達不能になる。
    CREATE 直後・ADD_POLICY 直後のプロセス再起動テストを含める）。VPD オブジェクトの実在に加え、
    **「JETUSE_QUERY へ SELECT 付与された全オブジェクト」と「全 `JETUSE_DS_*` 表」を実在から
    列挙**し、それぞれに登録簿の exact な 1 行と VPD ポリシーが揃うことを検証する
    （codex review-8 B003 — 旧実装は CREATE→GRANT→登録の順のため、「GRANT 済み・未登録」の
    表が既存環境に残りうる。登録簿 `ready` 行だけの検証ではこれを見逃す）。
    **不明・不整合の表が見つかったら dbchat / datasets 経路を 503 で停止**（他機能は起動継続）し、
    人間承認の下で revoke / 隔離 / 削除を選択する。初回移行では旧作成順の
    「未登録 GRANT 済み表」fixture を使った実 ADB テストで、検出 → 503 → 整理後に解除、を確認。
    途中失敗した一括付与から再実行で収束することも実 ADB でテスト。
    **コンテキストの set/clear 契約**（codex review-6 M006 — プール接続は再利用される）:
    JETUSE_QUERY 接続を取得するたび、SQL の parse 前に**必ず**その リクエストの owner で
    `SET_CONTEXT` を上書きし、設定失敗時は SQL を実行しない。finally で CLEAR_CONTEXT して
    から接続を返却する。テスト: 同一物理接続を「owner A → 例外 → owner B」で再利用しても
    A のコンテキストが残らない（越境 0 行）。
    **VPD 基盤（本項の DB オブジェクト・人間承認・完全性検証・set/clear 契約）の所有タスクは
    SP2-02**（codex review-7 B001 — SP2-02 の registry-first 作成が「CREATE TABLE → VPD →
    GRANT」の順で VPD 付与に依存するため。**ADB での DBMS_RLS 利用可否も SP2-02 冒頭で
    実機確認**し、不可なら本節を ADR 起票へ差し戻して人間判断〔spec-driven — 走査だけで
    境界を名乗らない〕。SP2-03 は敷かれた基盤の上にルートと層2ゲートを重ねる）。
    テスト: ADD_POLICY 失敗経路・コンテキスト未設定 0 行・**強制 tag 衝突の 2 owner 間で
    動的 SQL 越境が 0 行**（実 ADB）。
  - **層2（fail-closed の SQL ゲート — 早期 403）**（codex review-7 B002 で UX 補助から格上げ）:
    `execute_readonly` に呼び出し元 owner キーを必須引数で渡し、ユーザー入力 SQL を
    実行前に検査して次を**拒否**する: (a) 当人の登録簿に無い `JETUSE_DS_` 識別子、
    (b) **データディクショナリ/動的ビュー**（`ALL_` / `DBA_` / `USER_` / `CDB_` / `V$` /
    `GV$` 接頭辞の識別子 — **VPD は基表の行を隠すだけで辞書のメタデータ〔他 owner の表名・
    列名〕は隠さない**ため、辞書経由の越境をここで塞ぐ）、(c) パッケージ/関数のスキーマ修飾
    呼び出しと既知の動的 SQL ベクタ（`DBMS_` / `UTL_` / `SYS.` / `DBMS_XMLGEN` 等）、
    (d) DB リンク（`@`）・synonym 経由の間接参照。判定できない参照は**拒否側に倒す**
    （fail-closed）。SH 許可リストと一般的な SQL 関数・キーワードは許可する。
    行データの境界は層1（VPD）、**辞書・パッケージ面の境界は層2**という多層で成立させる。
    敵対的テスト: `ALL_TAB_COLUMNS` / `DBMS_METADATA` 等から他 owner の表名・列名が
    取得できない（403）ことを含める。
  - **呼び出し元の移行契約**（codex review-3 M004 — 公開シグネチャ変更の全経路。
    owner キーはすべて **§3.2.1 の導出ヘルパー経由**で渡す — codex review-7 M003）:
    `POST /api/dbchat/execute`（user SQL・owner = ヘルパー(user.subject)）／デモ経路 execute
    （owner = ctx.namespace）／`GET /api/dbchat/preview` → `preview_table`（ユーザー指定表名 —
    owner 検査対象）／`datasets.preview`（登録簿で本人検証済みだが同関数経由に統一）／
    agents の `query_database` ツール（`jetuse_core/tools.py`）／Fn ルーター（ARCH-02）の
    execute 相当。
    **agent 経路の扱い**（codex review-8 B002 — `packages/agent-containers/agent_db.py` は
    `execute_readonly` を使わず JETUSE_QUERY へ直結する独立経路で、invoke 契約に owner が無い）:
    (1) **データ行は VPD の fail-closed が自動遮断**する — owner コンテキストを設定しない
    セッションではポリシーが必ず 0 行を返す（層1 の default-deny がこの経路の存在価値）。
    (2) **辞書・パッケージ面**は、`agent_db.py` と `tools.py` にも**層2の共通 SQL ゲートを
    owner なしモードで適用**する（owner を持たない経路では `JETUSE_DS_` 参照・辞書ビュー・
    パッケージ呼び出しを**全部拒否** — SH 等の共有スキーマ照会という本来用途だけを通す）。
    (3) invoke 契約への owner_key 伝播（agent から本人 datasets を使えるようにする）は
    **SP3 の課題として明記**し、SP2 では対応しない。
    テスト（期待値は**層2の拒否に統一** — codex review-10 M005: 同一経路に VPD 0 行と
    層2 403 を同時に期待しない）: agent 経路からの `JETUSE_DS_` 参照・辞書・パッケージは
    **ゲートで拒否**（エラー応答）・SH 照会の回帰なし。VPD の 0 行はこの経路では検証せず、
    層2を通さない JETUSE_QUERY 直接接続 fixture（§6 の敵対的テスト (ii)）だけで検証する。
    **SH 等の固定スキーマ専用経路（`get_schema_info` など、ユーザー入力 SQL を受けないもの）は
    別関数として維持**し、owner 必須関数と分離する（既存挙動を壊さない）。
    全経路の回帰テスト + user→demo / demo→user / demo A→demo B / user A→user B の
    4 方向の越境拒否テスト、および**敵対的テスト**（DBMS_XMLGEN 等の動的 SQL 迂回が
    VPD で 0 行になること — 実 ADB E2E）。
  **注**: この穴は main（Public）にも存在する。SP2 は dev 枝で直すため Public 配信には
  リリース線の別 PR（main 起点）が必要 — ステージ報告で人間へ residual として提示。
- capabilities の `dbchat` ディスクリプタにデモスコープルートを追記（SP1-01 の routes 実在テスト
  `test_capabilities.py` が乖離を検出する構造を維持）。
- **voice / minutes / translate / docunderstand / agents はデモスコープ化しない**（箱に紐づく永続状態を
  持たない、または SP3 の要件が未確定）。specs/17 §5 の主要 3 系統（chat / rag / dbchat）で SP2 は完了。
  必要になった時点で SP1-03/本節と同型で追加する（追加コスト = ルート + ディスクリプタ追記）。

## 5. Internal テナンシ分離 — Identity Domains 実接続（SP2-04）

### 5.1 アプリ側契約

- **設定契約（.env / 配備 env）**: `AUTH_REQUIRED` / `OIDC_ISSUER` / `OIDC_AUDIENCE` / `OIDC_JWKS_URL`。
  settings.py に全キー実装済み・auth.py の検証経路は SP1-03 で RS256 実トークン検証済み（ローカル JWKS）。
  SP2-04 の作業は**実 IdP 固有差分の吸収と実機確認**であり、原則コード変更は最小。
  `.env.example` に `OIDC_AUDIENCE` の行を追記する（現状 ISSUER / JWKS_URL のみ記載）。
- **既定値**: Internal 配備（dev 枝の配備設定 = OKE manifest / ORM スタックの env）では
  **`AUTH_REQUIRED=true` を既定**にする。コード上の `Settings.auth_required = False` は**変えない**
  （main/Public のセルフホスト既定を壊さない — specs/17 §7）。
- **fail-closed の強化**（codex review-2 B006 — 現行 auth.py は JWKS 未設定の 500 のみで、
  issuer/audience が空だと当該検証を無効化し、sub 欠落も subject="" で受理してしまう）:
  `AUTH_REQUIRED=true` のときは **`OIDC_ISSUER` / `OIDC_AUDIENCE` / `OIDC_JWKS_URL` の 3 つ全てを
  必須**とし、いずれか未設定なら 500（設定不備で検証を欠いたまま受理しない — 同一 IdP の
  別アプリ用トークンの受理を防ぐ）。検証後の `sub` claim が欠落/空なら 401。
  wrong issuer / wrong audience / missing sub の拒否テストを追加する（SP2-04）。
- **ユーザー属性の取り込みは最小**: `subject = sub` claim（実装済み）。表示名が必要な画面は
  claims の `user_displayname`（Identity Domains 固有 claim）→ 無ければ sub を使う。
  **ユーザーテーブルは作らない**（DB へ属性を保存しない。必要になるのは SP4 の公開者表示以降）。
- **issuer / audience / JWKS URL は仕様に固定値を書かない**（codex review-1 B005: `iss` は
  旧 IDCS 系では `https://identity.oraclecloud.com/` 固定・現行 Identity Domains では
  ドメイン URL と、**契約が世代で異なる**）。`OIDC_ISSUER` / `OIDC_JWKS_URL` は当該ドメインの
  well-known discovery の値を、`OIDC_AUDIENCE` は**アプリ登録の primary audience（resource/client
  設定）または実トークンの `aud` 実測値**を設定する（audience は discovery からは得られない —
  codex review-2 B006）。現行 `.env.example` の固定 issuer 行はプレースホルダ
  （`OIDC_ISSUER=<discovery の issuer をそのまま>` 形式）へ改める（SP2-04）。
  実値は人間から受領し `.env` / 配備 secret にのみ置く（コミット禁止）。
  E2E では**実トークンの `iss` claim と設定値の一致**を証跡に記録する。

### 5.2 人間側作業の一覧（IAM / Identity Domain は人間ゲート — CLAUDE.md）

| # | 作業 | 引き渡し物 |
|---|---|---|
| 1 | Identity Domain に JetUse 用アプリを登録（SPA からのログインに使う public client(PKCE) または confidential app。E2E 用にトークンを取得できる構成にする） | issuer / audience / JWKS URL / client_id（secret があれば安全な経路で） |
| 2 | テストユーザー 2 名の払い出し（越境 404 E2E 用） | ユーザー名と、実トークンの取得手段（テスト用アプリの password grant 許可、または人間が発行したトークンの受け渡し） |
| 3 | Internal 配備先の secret / env への実値投入（AUTH_REQUIRED=true 含む） | 配備環境で有効化された状態 |

エージェントは上記を**受け取って使うのみ**。Identity Domain / IAM の設定変更は行わない。

## 6. 受け入れ条件（タスク別 — tasks/SP2-0X.md の確定版と同一）

- **SP2-01**: migration 017〜021（§1.1 単文 5 ファイル）が fresh スキーマ適用・冪等再適用とも成功。
  demos.py の list/update/set_status（§1.3）と §2.1 の **CRUD 4 ルート（DELETE を除く** —
  §2.1 の孤児化防止。**DELETE の公開は SP2-02）** が存在し、単体テストで CRUD 往復・
  越境 404（存在秘匿の同一形）・public の読み取り可/書き込み不可・401・
  status/owner_sub の変更不可・deleting の 404・**PATCH の null 意味論**（§2.2 — 空 PATCH /
  各フィールドの明示 null / description クリア）を検証。既存テスト回帰なし・ruff クリーン。
  実 ADB E2E: 2 ユーザーで CRUD 往復・越境 404・migration 冪等適用。
- **SP2-02**: `DELETE /api/demos/{demo_id}` を §3.2 の後始末込みで初公開し、順序・冪等性・
  **exact な列挙**（登録簿 exact owner / store metadata exact 一致 / files 接頭辞 /
  原本 prefix — §3.2）で後始末する（select_ai / opensearch / 原本 / 未登録孤児含む）。
  §3.2.1 の**排他リース**を全 demo mutation に実装（**DBMS_LOCK の ADB 可否を冒頭で実機確認 —
  不可なら demo mutation/DELETE 経路 503 + ADR 差し戻し（fail-closed）**・commit を跨ぐ保持・
  後始末完走まで保持・lock ID 範囲と戻り値の全処理・finally RELEASE・リース専用小プール・
  timeout 秒 / call_timeout ミリ秒の単位明示と既定復元・scope 明示 API・
  owner キー導出ヘルパーの単射エスケープ）。
  **VPD 基盤（§4.3 層1 — DBMS_RLS 可否の実機確認・DB オブジェクト・初回人間承認・
  起動時完全性検証・set/clear 契約。codex review-7 B001 で SP2-02 所有）**・
  **dataset 作成の registry-first 化（creating/ready 状態 + `_ensure_meta` の冪等 ALTER と
  ready backfill〔DDL 順序は §3.2 手順 2〕+ 幽霊行の回収）**・delete_dataset の DROP 先行化・
  外部名への owner hash + reservation_id 埋め込みと**原本 put（登録前のまま・ledger で回収・
  Select AI 有効時は必須リトライ可能ステップ）**・**demo の select_ai RAG /
  datasets / opensearch 命名の完全ハッシュ化**（§3.2 手順 2・3d・3e）・**配備前の旧命名資源
  一括クリーンアップ移行と write-ahead 台帳 `demo_backend_targets`（migration 022/023）**（§3.2）・
  **箱あたり上限とアプリ全体 files 上限（§3.1 — 全経路カウンタ・受け入れ閾値の実測定義込み）**・
  **リース取得の 2 契約（mutation=deleting 404 / DELETE=deleting 受理）と再入契約（§3.2.1）**を含む。
  単体テスト（DB/GenAI 層 fake）で 状態遷移・途中失敗→再実行収束・store 削除失敗/
  原本削除失敗の中断(503)・**並行系**（§3.2.1 — mutation 中の DELETE 待機・deleting 後の
  mutation 404・同時 2 本 DELETE・未登録残骸の回収・tag 強制衝突の非波及・
  creating クラッシュ 3 点〔registry 直後 / CREATE 直後 / ADD_POLICY 失敗〕の回収）・
  **usage_log の保持**（§3.2 手順 4 の契約 — 削除後も行が残る）・上限超過 422 を検証。
  実 ADB+実 GenAI E2E: デモ作成 → RAG upload（store lazy 生成・原本の実在確認）→
  datasets 投入（表実在。**API は SP2-03 のため `datasets.create_dataset(namespace, ...)` を
  直接呼ぶ fixture を使う**）→ **select_ai バックエンドで質問を実行して profile/index/$VECTAB を
  実在させる**（作らずに「不存在=成功」で通る偽陽性を防ぐ — codex review-2 M002）→
  **クラッシュ相当の未登録孤児を作る fixture**（期待 metadata〔sha1(owner キー)〕の store +
  `file_key` 規則の file を DB 未登録で作成 — codex review-4 M004 / review-18 B003）→ **demo_id 付き会話・messages・usage_log を
  SQL fixture で投入**（作成 API は SP2-03 のため — codex review-7 M006）→ DELETE →
  表・store（未登録分含む）・登録簿行（JETUSE_DATASETS / rag_stores / rag_files）・
  Select AI profile/index・原本 prefix 空・**会話行と messages が明示チャンク削除で消え
  （§3.2 手順 4 — 「1 会話に大量 messages」fixture でチャンク commit 途中停止 → 再 DELETE が
  続きから完走、を含む）、usage_log は保持**され、再 DELETE が 404。
  加えて (a) **dataset 投入（DDL）と DELETE の並行シナリオ**（fake では DDL 暗黙 commit を
  再現できない — §3.2.1）、(b) **15 秒超リース保持中の DELETE 待機**（call_timeout 実機確認。
  WAIT 境界付近のタイムアウトも）、(c) **一覧ページネーションの完走**（CP store 一覧 /
  DP files 一覧を小さいページサイズで複数ページ化し、末尾ページの対象を選別できること —
  codex review-5 M004）、(d) **箱あたり上限フル状態の DELETE 所要時間の実測**（同期方式の
  タイムアウト内 — §3.1）を実 ADB/実 GenAI で確認。OpenSearch は E2E 環境で無効なら
  SKIPPED 明記。使用 store は E2E 内で削除し枠を返す。
- **SP2-03**: §4.3 のルート + `POST /api/demos/{id}/conversations`（§4.2）が存在し
  capabilities の dbchat ディスクリプタと一致（routes 実在テスト緑）。
  **SP2-02 が敷いた VPD 基盤の上に層2の fail-closed SQL ゲート**（§4.3 — 辞書ビュー・
  DBMS_/UTL_・synonym/@・パッケージ呼び出しの拒否 + 登録簿外 `JETUSE_DS_` 403）と
  ルートを重ねる。**demo nl2sql のモデルは config 固定**（非所有者のモデル指定は無視 —
  §4.3）。単体テストで
  所有者の NL2SQL 往復（モック）・越境 404・**越境防止（§4.3 — 4 方向 403、
  本人データセット + SH は従来どおり、`execute_readonly` 全呼び出し元の回帰、
  辞書ビュー/パッケージ/synonym 参照の 403、非所有者のモデル指定が無効）**・
  **public デモの非所有者**（読み取り系 200 が demo namespace で動く/書き込み系 404）・
  §4.2 の会話検証（他人/他デモの conversation_id 404・user 経路との両方向持ち込み 404・
  user 単位 API 全 verb の demo_id IS NULL 強制・**demo chat が OCI Conversation を作らない・
  デモ間で LTM が混ざらない**・既存挙動の回帰なし）。
  実 ADB E2E: デモ箱の datasets へ日本語質問→正しい SELECT と結果、別ユーザー 404、
  **敵対的テストを 2 系統に分離**（codex review-8 M004 — 片方だけでは層1欠落を層2の 403 が
  隠す）: (i) **API 経由** — `DBMS_XMLGEN`・辞書ビュー・パッケージ参照が層2で **403**、
  (ii) **JETUSE_QUERY 直接接続 fixture（層2を通さない）** — owner A のコンテキストで
  owner B の表を動的 SQL 参照して **VPD が 0 行**。
  demo 会話の作成→**第 2 ターンで全履歴 + 新規発話を送り第 1 ターンを踏まえた応答**
  （§4.2 の継続契約）が箱に閉じる、既存 `/api/dbchat` 回帰なし。
- **SP2-04**: 実 Identity Domain 発行トークンで 認証なし 401 / 実トークン 200 /
  別ユーザーのデモ 404 を実機確認（SP1-03 SKIPPED.md の解消）。**実トークンの `iss` と
  `OIDC_ISSUER` 設定値の一致を証跡に記録**（§5.1）。auth.py の fail-closed 強化（§5.1 —
  AUTH_REQUIRED=true で issuer/audience/JWKS 全必須=不備 500・空 sub 401）と
  wrong issuer / wrong audience / missing sub の拒否テスト。`.env.example` の issuer 行を
  プレースホルダ化 + `OIDC_AUDIENCE` 追記。Internal 配備設定で AUTH_REQUIRED=true が既定。
  既存テスト回帰なし・ruff クリーン。

**ステージ完了条件**: 実環境で「デモ作成 → 箱の lazy 生成（RAG store / datasets 表 / 会話）→
デモスコープ操作（chat/rag/dbchat）→ デモ削除で §3.2 の後始末完走」の一連が確認できること。
他ユーザーのデモ id は全ルートで一貫して 404。`dev` が常時デプロイ可能（回帰なし）。

## 7. 非ゴール

- SP3（ビルダー・データ生成・provisioning/failed 遷移の生成側）・SP4（マーケット・published）。
- 物理スキーマ分離（CREATE USER per demo — 比較ドキュメントで不採用の根拠を記録）。
- eager プロビジョニング。デモ会話の一覧 API（SP3）。ユーザー属性の DB 保存。
- voice/minutes/translate/docunderstand/agents のデモスコープ化。
- Public（main）の認証既定・user 単位ルートの挙動変更。
