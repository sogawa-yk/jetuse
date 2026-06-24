# specs/07 — Phase 2 コアチャット（CHAT-01〜04）

状態: ドラフト（2026-06-10作成。CHAT-01/03を先行、CHAT-02/04は順次追記）
仕様参照: specs/00（LLM接続・モデル方針・SSE経路）/ ADR-0002（会話状態）/ ADR-0003（keepalive）

## [CHAT-01] Responses API統合サービス層

### 目的

SPIKE-01で実証した2系統API（Responses=gpt-oss/llama、Chat Completions=Gemini）を単一の抽象でラップし、SSEストリーミング・リトライ・usage記録を備えたチャット基盤を作る。

### 設計

- `jetuse_core/models.py`: モデルレジストリ。キー→ {OCIモデルID, API系統, 表示名, 既定パラメータ}。specs/00の3段構成（標準=gpt-oss-120b / 軽量=llama-3.3-70b / 高品質=gemini-2.5-pro）+ gemini-2.5-flash
- `jetuse_core/chat.py`: `stream_chat(model_key, messages, temperature) -> Iterator[ChatEvent]`
  - ChatEvent: `{"delta": str}` | `{"usage": {...}}` | `{"error": str}`
  - Responses系: `responses.create(stream=True)` の `response.output_text.delta` を変換
  - Chat Completions系: `chat.completions.create(stream=True)` の `choices[0].delta.content` を変換
  - 接続確立失敗は1回リトライ。usageは構造化ログにも記録（ADB永続化はCHAT-02）
- 認証: `AUTH_MODE=resource_principal`（CI/Functions上）/ 既定user principal（開発機）。`jetuse_core/genai.py` の `_signer()` で切替
- `service/main.py`: `POST /api/chat/stream`（要認証）
  - body: `{"model": str, "messages": [{role, content}], "temperature": float?}`
  - SSE応答: `data: {"delta": ...}` 連続 → `data: {"usage": ...}` → `data: [DONE]`
  - **無通信15秒でkeepaliveコメント送出**(ADR-0003。gemini-2.5-proのTTFT 10秒超対策)

### 完了条件

- [ ] ローカル: pytest（レジストリ/SSE整形/認可）+ 実LLMストリーミング確認
- [ ] 実機: API GW経由でJWT付き `/api/chat/stream` がgpt-oss/llama/geminiの3モデルでストリーミング応答（リソースプリンシパル署名）

## [CHAT-03] チャットUI

### 設計

- `pages/chat.tsx` を実装に昇格: メッセージリスト、送信、**ストリーミング表示**（fetch + ReadableStreamでSSEパース）、**中断**（AbortController）、**再生成**、アシスタント応答の**コピー**
- **Markdownレンダリング**: react-markdown + remark-gfm（コードブロックは等幅+背景。シンタックスハイライト/MermaidはCHAT-03b以降）
- モデル切替セレクタ（レジストリと対応）。temperatureはCHAT-04のパラメータUIで拡張
- API呼び出しは同一オリジン `/api/chat/stream` + `Authorization: Bearer`

### 完了条件

- [ ] lint/build クリーン
- [ ] 実機（API GWのURL）: ログイン→チャット送信→ストリーミング表示→中断/再生成/コピー動作

## [CHAT-03b] コードハイライト + Mermaidレンダリング

### 設計

- **シンタックスハイライト**: rehype-highlight（highlight.js共通言語サブセット、自動言語検出は無効=指定言語のみ）。`.md pre` は既存のダーク背景を維持し、テーマCSSは `github-dark` をベースに背景を透過
- **Mermaid**: ` ```mermaid ` コードブロックをSVGレンダリング
  - mermaid本体（重量級）は**動的import**でチャンク分離し、mermaidブロック出現時のみロード
  - ストリーミング中の未完成ソースを考慮し、`mermaid.parse` で**検証が通った時のみ**描画。不正・未完成の間はコードブロック表示のままにする（エラーフラッシュさせない）
  - ダークモード切替に追随（`theme: dark/default` で再描画）。`securityLevel: 'strict'`
- 実装は `components/markdown.tsx` の `Md` コンポーネントに集約し、chat.tsx から利用

### 完了条件

- [ ] lint/build クリーン（mermaidが初期バンドルに含まれないこと=チャンク分離を確認）
- [ ] 実機: コードブロックのハイライト表示、mermaidフロー図のSVG描画、ダークモード表示

## [CHAT-02] 会話永続化

### 目的

会話履歴の正をADB（jetusedev）に置く（ADR-0002）。一覧・再開・削除・ユーザー分離・usage永続記録を実現する。実装は**案A: CI（FastAPI）に同居**（ユーザー承認2026-06-10。Functions移行は後続）。

### スキーマ（マイグレーション 001）

- `CONVERSATIONS(id VARCHAR2(36) PK, owner_sub, title, model, created_at, updated_at)`
- `MESSAGES(id, conversation_id FK ON DELETE CASCADE, seq, role, content CLOB, created_at)`
- `USAGE_LOG(id, owner_sub, conversation_id, model, input_tokens, output_tokens, created_at)`
- `SCHEMA_MIGRATIONS(version PK, applied_at)` — `jetuse_core/migrations/*.sql` を順番適用するランナー（`python -m jetuse_core.migrate`）

### 接続設計

- 専用DBユーザー `JETUSE_APP`（自スキーマのみ。ADMINはマイグレーション時のみ使用）
- コンテナからは**起動時に非公開バケットからmTLSウォレットを取得**（リソースプリンシパル / 開発機はユーザープリンシパル）して oracledb 接続プール生成
- パスワード類は環境変数（devはtfvars sensitive。Vault化はPhase 8）

### API（CI同居）

- `GET /api/conversations` 自分の一覧（updated_at降順）
- `POST /api/conversations` `{model, title?}` → 作成
- `GET /api/conversations/{id}` メッセージ込み取得（所有者のみ）
- `DELETE /api/conversations/{id}`（所有者のみ）
- `POST /api/chat/stream` 拡張: `conversation_id` 任意。指定時はユーザー発話を保存→応答完了時にアシスタント応答とusageを保存、updated_at更新。タイトルは初回ユーザー発話の先頭30字

### UI

- チャットページに履歴サイドバー（一覧・新しい会話・切替・削除）。選択で復元

### 完了条件

- [ ] pytest（リポジトリ層はfake、認可分離のテスト含む）/ lint / build クリーン
- [ ] 実機: マイグレーション適用 → API GW経由で 作成→チャット→リロード相当の再取得→削除 のE2E
- [ ] 他ユーザーの会話が見えないこと（owner_sub分離）を実機確認

## [CHAT-04] パラメータ設定UI + システムプロンプトプリセット

- チャットページに設定パネル（⚙トグル）: temperatureスライダー（0〜2、既定はモデル定義値）、システムプロンプトのtextarea
- システムプロンプトは送信時に `role=system` として先頭に付与（msgs状態・ADBには保存しない — 会話ごと保存はCHAT-06以降で検討）
- プリセット: `PROMPT_PRESETS(id, owner_sub, name, content)`（migration 002）。`GET/POST /api/presets`, `DELETE /api/presets/{id}`。UIで選択/保存/削除
- API GWはルート増殖を防ぐため `/api/{p*}` キャッチオール→CI に整理（`/api/chat/{p*}` はSSE用300sで維持。具体的なルートが優先されることを実機確認）

## [CHAT-05] 履歴検索 + タイトル自動生成

- 検索: サイドバーの検索ボックス（クライアント側フィルタ。サーバー側 `?q=` は件数が増えるPhase 3以降で）
- タイトル自動生成: 初回の応答完了後にUIが `POST /api/conversations/{id}/title` を呼ぶ → サーバーがllama（高速）で15字以内のタイトルを生成して更新・返却
- **共有リンクはこのタスクから除外**（読み取り専用公開の認可設計が必要なため別タスク化。Phase 2出口までに要否判断）

## [CHAT-06] 短期メモリ統合（必須・ユーザー指示2026-06-10）

Enterprise AI AgentsのConversations+履歴圧縮を採用してトークン消費とレイテンシを削減する。ADBの会話レコードにconversation idを紐付け、履歴の正はADBのまま（ADR-0002追記参照）。retention設定とstore挙動（既定でサーバー保存される実機挙動）の設計を含む。CHAT-02完了後に着手。

## [CHAT-03c] チャットUX改善（CP②ユーザーフィードバック 2026-06-11）

1. **スクロール追従の解除**: 生成中もユーザーが上にスクロールできる。最下部付近（80px以内）にいる時のみ自動追従し、上に離れたら追従停止（ChatGPT/Claude同等）。送信時は追従を再開
2. **Terraform/HCLハイライト**: highlight.js共通セットにHCLが無いため、カスタム文法（terraform/tf/hcl）を登録
3. **mermaid自動修復**: LLM頻出の構文ミス「ノードラベル内の未クオート括弧」（実測: gpt-ossが `C[POST (Power‑On Self Test)]` を生成）を、parse失敗時に自動クオートして再試行。それでも不正ならコード表示＋「構文エラーのためコード表示」注記（モデル起因と分かるように）
4. **temperature上限を1.5に変更**: 実測でgpt-ossは1.9以上で推論が暴走し本文が出ない（1.9: 出力1,970tokが全て内部推論・本文ゼロ / 2.0: 90秒無応答）。空応答時はバブルに注記を表示

## [CHAT-04b] 生成パラメータの拡張（ユーザー承認 2026-06-11）

実機対応マトリクス（docs/tips.md）に基づき3項目を追加。**API系統で対応が割れるためUIはモデルで出し分け**。

- **top_p**: スライダー 0〜1（step 0.05、未操作時は送信しない=モデル既定）。両系統対応
- **最大出力トークン**: 数値入力（空=無制限/モデル既定）。Responses系は `max_output_tokens`、Chat系は `max_tokens` にマップ
- **reasoning effort**: low/medium/high のセレクタ。**推論モデル（gpt-oss）選択時のみ表示**。Responses系の `reasoning: {effort}` にマップ
- レジストリに `reasoning: bool` を追加し、`GET /api/chat/models` が `api`/`reasoning` を返してUIが出し分けに使う
- バリデーション: top_p 0<x≦1、max_tokens 1〜32768、effort enum。未指定はAPIに渡さない
- stop / penalties / seed は要望が出るまで見送り（Chat系のみ対応のため）
- 完了条件: pytest（パススルー/バリデーション）/ 実機: 3パラメータ付きSSEが各系統で動作、effort low/highでgpt-ossの出力トークン差を確認

## [CHAT-06b] 短期メモリ圧縮の有効化（2026-06-10夜間計測で発見）

Conversationオブジェクトのmetadataに未文書フラグ `short_term_memory_optimization`（既定 `"false"`）が自動付与されることを実機で発見。`"true"` で履歴圧縮（compaction）が発動する。

- 実測（gpt-oss-120b、8ターン・各300字回答）: 累計input **19,229 → 11,220（42%削減）**。圧縮はコンテキスト約2.5kトークン超で発動（input推移 2669→1225）
- **圧縮後もターン1の事実を正答**（記憶保持OK。中立事実プローブで両条件比較済み）
- 注意: 記憶プローブに個人情報様の内容（社員番号等）を使うとモデルが拒否回答するため計測には中立事実を使うこと（圧縮と無関係の交絡）
- 実装: `create_oci_conversation` のmetadataに `short_term_memory_optimization: "true"` を既定付与
- 完了条件: 実機でアプリ経由の長会話の入力トークンが圧縮で減少すること + 記憶保持

## [CHAT-07] DB接続タイムアウト + 503即時返却（backlog #11、2026-06-10）

実機障害: 夜間停止でADBがSTOPPEDのまま→DB接続が無限ハング→送信UIが完全無反応（タイムアウト未設定のため）。

- `jetuse_core/db.py`: 接続プールに `tcp_connect_timeout=5s` / `getmode=TIMEDWAIT, wait_timeout=5s` / `ping_interval=30s` を設定。`connect()` コンテキストマネージャで取得した接続に `call_timeout`（既定10s、`DB_CALL_TIMEOUT_MS`で調整）を設定し、実行中のDB停止でも往復が打ち切られるようにする。リポジトリ層は `connect()` を使用
- `service/main.py`: `oracledb.Error` の例外ハンドラで **503 `database unavailable`** を即時返却（ハングさせない）
- フロント: 会話作成POSTに15秒タイムアウト。失敗時は**ステートレスで会話を継続**し「履歴が保存されない」旨の通知を表示。SSEの503はエラーメッセージをバブルに表示
- 完了条件: pytest（503マッピング）/ 実機: ADB停止状態で `POST /api/conversations` が数秒で503、ADB再開でE2E復旧

## [CHAT-08] チャット停止時の上流キャンセル伝搬（backlog #2、2026-06-10）

UIの停止/切断後もサーバーが上流LLMストリームを完走しトークンを浪費する問題（main.py既知の制約コメント）。

- SSEジェネレータの `finally`（クライアント切断・GeneratorExit）で**キャンセルフラグ**を立て、produceスレッドのイベントループで検知して上流ストリーム(`close()`)を打ち切る
- 途中までの部分応答は従来どおりADBへ保存（usageは上流から取れた場合のみ記録）
- 完了条件: pytest / 実機: ストリーム途中で切断→構造化ログに `upstream cancelled` が出る・プロセスが滞留しない

## [CHAT-09] 会話削除時のOCI Conversation削除同期（backlog #3、2026-06-10）

- `DELETE /api/conversations/{cid}`: ADB削除前に `oci_conversation_id` を取得し、ADB削除成功後にOCI Conversations側を**ベストエフォート削除**（失敗はログのみ、APIは成功を返す — 履歴の正はADB、OCI側はretentionでも消えるため）
- 完了条件: pytest / 実機: 短期メモリ付き会話を削除→OCI側 `GET /conversations/{id}` が404
