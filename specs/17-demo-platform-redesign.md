# specs/17: JetUse デモ生成プラットフォーム 再設計 — SP1: JetUse API

> 状態: ドラフト（人間レビュー待ち）。日付: 2026-07-06。
> 設計判断: `docs/decisions/ADR-0015`（本再設計。ADR-0013 を置換）・`docs/decisions/ADR-0016`（ブランチ）。
> 本仕様は **SP1（JetUse API）** を詳細化し、SP2〜4 は分解と役割分担のみ概略で添える。

## 0. 位置づけ・背景

JetUse を「フィールドSAが、リファレンスアーキテクチャから外れずに、顧客業務に寄り添ったデモを短時間で作れる」
プラットフォームへ拡張する。2026-07-05 のリポジトリ方針転換（main のみへリセット）を受け、過去の
デモ生成プラットフォーム設計（ADR-0013 / specs/16）に縛られず**フレッシュに再設計する**（決定=C）。
main に生存する資産（認証コンテキスト・usecases の owner/visibility・manifest 署名・file_search/ベクタストア・
サンプルスキーマ実体化）は、合う所だけ日和見的に流用する（アーキの縛りにはしない）。

**2つの版**（`docs/guides/branching-and-releases.md` / ADR-0014・0016）:
- **Public 版**: 各ユーザーが自環境へセルフホスト。OCI の AI 機能を気軽に試すショーケース。`main` 配信。
- **Internal 版**: ベンダー（施主）が単一インスタンスをホスティングし、フィールドSA が Identity Domains
  認証でアクセス。ビルダー／マーケットプレイス／マルチテナントを上に重ねる。`internal-stable` 配信。

## 1. 全体像（サブプロジェクト分解）

各 SP は独立した 仕様→計画→実装 サイクルを持つ。

| # | サブプロジェクト | 内容 | 版 | 開発枝（merge先） |
|---|---|---|---|---|
| **SP1** | **JetUse API** | 既存能力を「デモ生成フロントが叩ける安定API面」に整理。能力追加を安く保つ。 | 共通 | `main`（→ sync `dev`） |
| SP2 | テナンシ + Demo エンティティ | Identity Domains でユーザー分離。`Demo`(owner/visibility) を一級化。デモ単位のデータ箱を生成。 | Internal | `dev` |
| SP3 | ビルダー | ヒアリング(NL)→能力の選択/配線→フロント生成(OpenCode + OCIモデル)→データ生成→Demo 産出。 | Internal | `dev` |
| SP4 | マーケットプレイス | 公開/配布。SP2 でデータモデルに `visibility` を仕込み後付け。 | Internal（将来） | `dev` |

> 「開発枝」は各 SP の作業を merge する先であり、**配信元ではない**（§7 / ADR-0016）。配信元は Public=`main`
> （tag `public-vX.Y.Z`）、Internal=`internal-stable`（`dev → internal-stable` リリース + tag `internal-vX.Y.Z`）。
> Internal 固有の SP2〜4 は `dev` に積み、リリース点で `internal-stable` へ落として本番配信する。

**基本方針の確定事項（全 SP 共通）**:
- 能力モデル: **既存 JetUse 能力の組み合わせのみ**。生成するのは**フロント + データ**。バックエンドは JetUse 固定。
- 実行時のデモ = **静的SPAバンドル**（デモごと）。JetUse が `/api/demos/{id}/...` 配下で配信し、ブラウザから
  ユーザー認証 + デモスコープで JetUse API を叩く。**デモ専用のサーバコードは持たない**。
- 秘密・外部接続は**デモ専用サーバではなく共有の `connector.invoke` 能力**で解く（秘密は Vault、サーバ側で
  JetUse が代理呼び出し）。デモ専用フル app（コンテナ）は将来の限定的エスケープハッチとしてのみ留保。

## 2. SP1 スコープ

SP1 = 下流（SP2/SP3）を動かすために JetUse API が提供すべき 3 要素。**既存ルートの全面書き換えではなく、
「どれを能力として公開し、どう記述し、どうデモ単位にスコープするか」の整理が本体**。

1. **能力カタログ** — ビルダーが読む機械可読な「メニュー表」。
2. **デモ向け安定API面** — どの既存ルートを「デモ合成可能な能力」として公開するかの確定。
3. **(user, demo) スコープの継ぎ目** — 呼び出しにデモが乗り、データがデモ単位に分離される seam。

## 3. 能力カタログ（要素1）

**方式 = 案1: 自動 OpenAPI + 手書きディスクリプタ**。

- FastAPI が自動生成する `/openapi.json`（技術契約: path/method/入出力スキーマ）を土台にする。
- その上に、デモ向け能力にだけ **手書きの能力ディスクリプタ**を1件持つ。フィールド:
  - `capability`（例 `rag.search`）／`summary`（何ができる）／`when_to_use`（デモでの使いどころ）／
    `example`（入力例→出力の要点）／`demo_safe`（デモ合成に出してよいか）／`route`（対応する OpenAPI path）。
- ビルダー(SP3)へは「OpenAPI（技術詳細）＋ ディスクリプタ（用途・例・安全フラグ）」を統合したカタログを返す
  1 エンドポイント（例 `GET /api/capabilities`）で提供する。カタログの**出力形は将来も不変**に保つ。
- **能力追加のコスト = ルート追加 + ディスクリプタ1件**。

### 3.1 仕様そのものの公開（API-01）

デモ作成者は **SDK ではなく仕様**を受け取り、そこからクライアントを生成する（契約が動いている
間は生成のほうが安全＝古い SDK が動いてしまう状態を作らない）。

**最も重要な利用者はコーディングエージェント**である（人が SDK を生成する話より優先）。理由:
人は 400 を食らってから試行錯誤できるが、**エージェントは仕様に無いことを推測（でっち上げ）する**。
実測（`docs/verification/API-01.md` §7）では、説明文の無い仕様だけを渡したエージェントは
「文書検索させる」依頼に対し `enabled_tools` を指定できず、**HTTP 200 を得たまま検索を行わない
リクエスト**を作った（起動したのは `code_interpreter`）。したがって仕様は次の 3 つを持つ:

1. **エラー応答**（401 / 400 / 404 / 409 / 413 / 422 / 502 / 503）の意味と形。
   **実装が返しうるコードを漏らさない**（漏らすと生成クライアントは障害時に型のない例外で落ちる）。
   401 は `require_user` を通る全ルートに共通で宣言する（`AUTH_REQUIRED=true` が公開スタックの既定）。エラーから自己修正するには
   エラーの意味が仕様にある必要がある。形は基本 `{"detail": "<理由>"}`。**422 だけ実装が 2 通り
   返す**ので `oneOf` で両方宣言する（スキーマ検証で落ちれば `detail` は項目ごとの配列
   `loc`/`msg`/`type`、ルート側の検証で落ちれば文字列）。片方だけ宣言すると生成クライアントが
   実応答を解けない。コードの意味は横断で固定し（`service/openapi_errors.py` に 1 か所）、
   ルート固有の事情だけを各ルートで足す。
2. **「いつ使うか」**（型だけでは使い分けが決まらない）。特に排他・依存・似た名前のパラメータ。
3. **例**（エージェントは例から形を学ぶ）。素のチャット・エージェント実行（文書検索あり）・
   文書 Q&A の 3 つ。置き場所は **schema 側が生の値の配列**・**名前と説明つきは requestBody 側の
   Example Object**（schema に `summary`/`value` を置くと、それ自体がリクエストの例に見える）。
   正本は 1 か所（`schemas.CHAT_REQUEST_EXAMPLES`）。**間違った例は害**なので、例が実ルートで
   受理されることをテストで固定する（`tests/test_openapi_spec.py`）。

トークンの入手方法も書く（**この API に発行の口は無い**と明記する — 書かないと
エージェントが発行エンドポイントを組み立てる）。

**SSE を返すルートは 200 を `text/event-stream` で宣言する**（`service/sse.py` の `SSEResponse` を
`response_class` に渡す）。既定の `application/json` のままだと、生成クライアントは
ストリームを JSON としてデコードしようとして壊れる。フレームの種類（`delta` / `citations` /
`tool_call` / `error` / `ka`）と `[DONE]` 終端も応答の説明に書く。**HTTP 200 のまま本文で
失敗しうる**ことを明記する（`error` フレーム）。

- 公開先は **`GET /api/openapi.json`**。API Gateway のキャッチオール `/api/{p*}` にそのまま乗るため
  **ゲートウェイのルート追加は不要**（実測: `/openapi.json` は SPA の Object Storage 側へ落ちるので
  ルート直下には置けない）。
- FastAPI 既定の `/openapi.json` `/docs` `/redoc` は**無効化**する。仕様を返す口を 1 本に限り、
  他の `/api/*` と同じ認証（`require_user`）を通す＝ `AUTH_REQUIRED=true` の配備で
  **仕様だけ無認証で晒さない**（fail-closed）。公開配備で無認証にするかの既定は ADR-0028。
- **二重管理にしない**役割分担: ワイヤ契約（path/method/スキーマ/パラメータの説明）の正本は
  OpenAPI、用途側（`summary` / `when_to_use` / `example` / `demo_safe` / 能力差）の正本はディスクリプタ。
  カタログは OpenAPI から `requestBody` / `responses` を**導出**する（上記のとおり）。
- **呼ぶ前に分かること**を仕様の責務に含める。パラメータ間の排他・依存・似た名前の使い分けは
  当該ルート/パラメータの `description` に書く（例: `agent` と `rag` の排他、エージェントの文書検索に
  `enabled_tools=["rag_search"]` が要ること、`rag_backend` と `agent_rag_backend` の違い）。
  型だけの仕様は配れない。

**将来の移行**: 能力が増えてディスクリプタの書式がブレ、ビルダーの生成品質が落ち始めたら、統一 Capability
インターフェース（各能力が metadata+入出力schema+invoke を実装しレジストリが自動カタログ化する「案2」）へ
寄せる。カタログの出力形が不変なので **SP3 は無改修**（内部の作り方だけ差し替え）。

## 4. 公開する能力（要素2）

**デモ向け能力（カタログに `demo_safe=true` で載せる = 生成フロントが叩ける）**:

| 能力 | 内容 | 既存ルート |
|---|---|---|
| `chat` | LLM 対話（ストリーミング） | `routes/chat.py` |
| `rag.search` | 文書検索Q&A（引用付き） | `routes/rag.py` |
| `dbchat` | 自然言語→SQL でデータ照会 | `routes/dbchat.py` |
| `agents` | エージェント/ツール実行（**デモ固有の外部 HTTP API をツールとして渡せる** — 下記） | `routes/agents.py` |
| `voice` | STT/TTS・文字起こし | `routes/voice.py` |
| `minutes` | 議事録（文字起こし+要約） | `routes/minutes.py` |
| `translate` | 翻訳 | `jetuse_core/translate.py` |
| `docunderstand` | 文書理解・抽出 | `jetuse_core/docunderstand.py` |

**裏方（カタログに載せない）**: admin / conversations（履歴CRUD）/ tools / mcp_servers / datasets / embeddings /
moderation / guardrails（自動適用の横断機能）。

### `agents` の外部ツール（TOOL-01・2026-08-01 実装済み）

デモ側が持つ**素の HTTP エンドポイント**を、名前・説明・JSON Schema つきで登録し、エージェント実行時に
組込ツールと同列に配線できる。**デモ専用のサーバコードを JetUse 側に持たない**（§1）を守ったまま、
業務ロジックを AI に使わせるための「渡す口」だけを一級機能にしたもの。

```
POST /api/agent/http-tools   name / description / parameters(JSON Schema) / url / method / 認証
                             / headers(固定ヘッダ) / idempotency_header（TOOL-02）
POST /api/chat/stream        agent=true + http_tool_ids=[...] で実行に配線
```

- モデルが呼ぶと **JetUse がサーバ側で HTTP を代理実行**して結果を返す（ブラウザから直接叩かせない）。
- 秘密は **Vault に置き OCID で参照**する（`mcp_servers.auth_secret_ocid` と同じ流儀。新方式を作らない）。
  ヘッダ名だけ選べる（既定 `Authorization`）。DB にも API 応答にも平文は現れない。
  **使える秘密は、本アプリのコンパートメントにあり freeform タグ `jetuse_tool_owner` が登録者と
  一致するものだけ**。これが無いと「サービスの権限で読める任意の秘密を、利用者が指定した外部 URL へ
  送らせる」経路（confused deputy）になる。
- **認証以外の必須ヘッダ（TOOL-02・2026-08-02 実装済み）**: `headers` に「毎回この値を付ける」
  固定ヘッダを最大 5 個（値は印字可能 ASCII 200 文字まで）。`idempotency_header` は**ヘッダ名だけ**
  登録すれば、**呼び出しのたびに JetUse が新しい値（uuid4）を発行**して送る（モデルには作らせない
  ＝使い回しによる二重実行防止の無効化を避ける。ADR-0023）。動的な値の一般テンプレート機構は持たない。
  - 組み立て順は **固定 → 冪等 → 認証 → Host** で、後から入るものが勝つ＝固定ヘッダで認証・宛先を
    上書きできない。禁止ヘッダ（`host` / `authorization` / `proxy-*` / `cookie` / `set-cookie` /
    `content-length` / `content-type` / `transfer-encoding` / `accept-encoding` / `connection` /
    `upgrade` / `expect` / `te` / `trailer` / `keep-alive`
    ＋そのツール自身の `auth_header`）・CR/LF 混入・個数/長さ超過は**登録時と実行時の両方で拒否**。
    ※ `content-encoding` など上記以外の `content-*` は禁止していない（枠組みを決めるのは長さと型なので、
    そこだけを塞ぐ。応答の圧縮は `accept-encoding: identity` 固定で別途扱う）。
  - ヘッダ名は RFC 9110 の token（`X_Trace` / `api.version` のような名前も可。区切り文字・制御文字は不可）。
  - **固定ヘッダの値は DB に平文で保存される。秘密を入れないこと**（認証は Vault 参照を使う）。
    一覧 API は値を返さず**名前だけ**返す（`header_names`）。DB の値が壊れている行は
    `headers_invalid: true` で示し（隠さない）、**一覧は 200 のまま・その行の実行だけが 400** になる。
- **入れ子オブジェクトと配列（TOOL-03・2026-08-02 実装済み）**: `parameters` に `object` / `array` を
  宣言できる。業務 API のボディは入れ子と配列が普通で、平坦なスカラーだけだと**複雑な API ほど
  渡せない**という逆転が起きるため（実案件で 8 本中 6 本が登録不可だった）。
  受理するのは**実行時に同じ強さで検証できる形だけ**（ADR-0024）:
  - `object` は **`properties` を持つこと**（自由形式は受理しない＝検証できない）。root だけ省略可。
  - `array` は **`items`（単一スキーマ）を持つこと**（タプル形式の `items` は受理しない）。
  - 未対応の JSON Schema キーワード（`enum` / `pattern` / `oneOf` / `$ref` 等）は**各階層で落とす**。
    素通しすると「モデルには制約に見えるが実行前検証は素通し」になる。
  - 実行前検証も再帰。**未知キーの拒否・型検査・`required` を各階層で**効かせ、配列は要素ごとに
    `items` で検査する。内側の違反は**相手へ送る前に**拒否する。
  - 上限（超過は**黙って切り詰めない**）: 入れ子の深さ **6 段**・スキーマ全体 **100 ノード**・
    `MAX_PROPERTIES` **20 を各階層に**（以上は登録時に 400）／配列の要素数 **100 件**
    （実行時にツール実行の失敗として返す）。
  - **GET ツールには入れ子・配列を宣言できない**（登録時に 400）。GET にはボディが無く、
    入れ子をクエリ文字列へ載せる標準の書き方が無いため。入れ子が要る API は POST で登録する。
- **SSRF は fail-closed**: https 必須／内部メタデータ・ループバック・私有レンジ・URL 埋め込み認証情報を
  登録時と実行時の両方で拒否／リダイレクトを追わない。
- タイムアウト 15 秒・応答 128KB・リトライ 0・1 エージェント 8 ツールまで。上限超過は黙って切り詰めず
  「ツール実行が失敗した」としてモデルへ返す。
- **MCP サーバー登録とは別経路として共存**する（MCP は OCI 側でサーバーサイド実行）。
- **既知の制約: 呼び出し先 URL は平文で保存され、API 応答にも現れる**（受容した residual）。
  秘密は Vault だけ、という建前と食い違うケースがある: **PAR や署名付き URL は
  パス・クエリ自体が資格情報**なので、それを URL 欄に登録すると「秘密が平文で保存・表示される」。
  **そういう URL を登録しないこと。** 認証が要る相手には URL ではなく Vault 参照（上記）を使う。
  対処を入れなかった理由: デモ用途では URL を隠すと登録内容の確認・切り分けができなくなり、
  実害（登録者本人しか見られない）に対して代償が大きいと判断した（2026-08-01 人間ゲートで受容）。
  将来 PAR を扱う要求が出たら、URL のマスクか PAR 形式の登録拒否のどちらかを入れる。

検証: `docs/verification/TOOL-01.md` / `TOOL-02.md` / `TOOL-03.md`。能力カタログ（`/api/capabilities` の `agents.external_tools`）には
実測できた範囲だけを載せる。

**将来足す能力**: `demo データのプロビジョニング`（デモ専用スキーマ作成 + データ投入。
SP3 のデータ生成の着地先・SP2 寄り）。
※ `connector.invoke`（秘密・外部接続）は上記 `agents` の外部 HTTP ツール（TOOL-01）で満たした。

**既存 usecases の扱い = A（存続）**: usecases（fields+template の自作ミニアプリ）は **Public 版のショーケース
機能として存続**する。Internal のビルダーとはペルソナ・用途が別物であり、統合しない。

## 5. (user, demo) スコープの継ぎ目（要素3）

**`DemoContext` seam**: 呼び出しのたびに共有の依存関数が
1. `demo_id` を受け取り、2. **認証ユーザーがそのデモの所有者か（or 公開済みか）を検証**し、
3. デモの箱の実体（DBスキーマ名・RAGストアID・会話名前空間等）を束ねた `DemoContext` を返す。

各能力は生の `user` ではなく `DemoContext` を受け取り、**その箱の中だけ**を操作する。所有権検証を通らない
呼び出しは 404/403 で弾く（データ分離は信頼境界。fail-closed）。

**箱の分け方（既存資産流用）**:
- RAG → デモごとに別ベクタストア（file_search をデモ id で名前空間分け）。
- DB → デモごとに別スキーマ `demo_<id>`（サンプルスキーマ実体化の仕組みを流用）。
- 会話 → `demo_id` で紐付け。

**demo_id の渡し方 = パス**: `/api/demos/{demo_id}/rag/search` のようにパスへ含める（**付け忘れ防止**＝
スコープ漏れをルーティングで構造的に防ぐ）。Public 用の user 単位ルート（`/api/rag/search` 等）は現状のまま
共存させる。

**役割分担**: SP1 は **継ぎ目（`DemoContext` 依存関数と、能力がそれを受け取る形）**まで敷く。**実際の Demo
エンティティの保存・箱のプロビジョニングは SP2**。

## 6. SP2〜4 概略（本仕様の詳細対象外）

- **SP2**: `Demo`(id, owner_sub, visibility, name, config, created_at…) を DB に保存（usecases の owner/visibility
  パターンを踏襲）。デモ作成時に箱（スキーマ・ベクタストア）をプロビジョニング。`DemoContext` の解決先を実装。
- **SP3**: ヒアリング → 能力カタログを LLM に渡してデモ設計 → OpenCode + OCI モデルで静的SPA生成 →
  サンプルデータ生成・投入 → `Demo` として保存。生成フロントは JetUse API を叩くだけ（バックエンド生成なし）。
- **SP4**: 中央レジストリ + 署名（manifest 署名の既存実装を土台）。`visibility=published` のデモを配布。

## 7. ブランチ / リリース

3 長期ブランチ（詳細は `docs/guides/branching-and-releases.md` / ADR-0016）:
- `main` = Public 安定版・Deploy ボタン配布元（常時デプロイ可）。
- `dev` = Internal 統合（開発）。`main ⊆ dev`。
- `internal-stable` = Internal 安定版（新設）。施主のホスト本番が追う。`dev → internal-stable` でリリース。

SP1 は `main` 発（Public flow）→ `dev` へ sync。SP2〜4 は `dev` 発。

## 8. 非ゴール（SP1）

- Demo エンティティの保存・プロビジョニング（SP2）。ビルダー（SP3）。マーケットプレイス（SP4）。
- `connector.invoke` の実装（将来能力）。統一 Capability インターフェース（案2 は将来移行）。
- 既存能力の内部ロジックの作り替え（SP1 は公開面・カタログ・seam の整理に限定）。

## 9. 受け入れ条件（SP1 完了ゲート）

- `GET /api/capabilities` が 8 能力のカタログ（OpenAPI 由来の技術詳細 + 手書きディスクリプタ）を返す。
- 8 能力それぞれに `demo_safe=true` のディスクリプタが存在し、裏方ルートは載らない。
- `/api/demos/{demo_id}/...` 配下の能力が `DemoContext` を経由し、**他ユーザーのデモ id では 404/403** に
  なる（所有権検証の実機/テスト確認）。Public 用 user 単位ルートは従来どおり動作。
- 既存の Public 機能（usecases 含む）が回帰なく動き、`main` が常時デプロイ可能を維持。
- area の test/lint 緑 + 実環境 E2E（能力カタログ取得 + デモスコープ越境拒否）通過。
