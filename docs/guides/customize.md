# カスタマイズガイド（DOC-03）

本アプリを社内要件に合わせて拡張・改変する際の手引き。コードを書かずにできるもの（①②）から
順に難易度が上がる。

## ① ユースケースの追加（コード不要・UI操作）

- アプリの **ビルダー**（ナビ「ユースケースを作る」/ `/builder`）で、フォーム項目とプロンプト
  テンプレート（`{{変数名}}` 置換）を定義して保存・共有。モデルも選べる
- 組み込みの初期ユースケースを増やすには `packages/api/jetuse_core/usecases_builtin.py` の
  `BUILTIN_USECASES` に定義を追加（フォーマットは既存エントリに準拠。definition JSONが正）

## ② ブランディング差し替え（コード不要・設定ファイル）

- `packages/web/public/branding.json`（実行時読み込み）で `shortName` / `logoText` / 色を変更
  → ビルド不要で反映（`branding.ts` がCSS変数 `--brand-*` に流し込む）
- ロゴ画像や配色はRedwoodトークン（`packages/web/src/styles/tokens.css`）の範囲で調整。
  色のハードコードは避け、`theme.css` の意味トークン経由で変える（UI改修方針）

## ③ モデルの追加・変更

- `packages/api/jetuse_core/models.py` の `MODELS` にエントリ追加:
  - `oci_id`（OCIのモデル名）/ `api`（`responses` か `chat`）/ `label` / `vision` 等
  - **API系統はモデル依存**（実機確認必須）。Responses不可なら `chat` にする（llama系の例）
  - 画像対応は実機で確認したものだけ `vision=True`（gpt-ossは受理するが見えない等の罠あり）
- 大阪リージョンの提供モデルは変動する。`oci generative-ai model-collection list-models` で確認

## ④ ツールの追加（エージェント）

- `packages/api/jetuse_core/tools.py` の `TOOLS` に `ToolDef` を追加:
  - `name` / `description`（LLMが選ぶ判断材料）/ `parameters`(JSON Schema) / `handler` / `requires_approval`
  - ハンドラは同期関数。外部アクセスはSSRF・タイムアウトに注意（既存 `web_fetch` 参照）
- 3エンジンへの反映:
  - **native**: 自動で利用可能（承認フロー対応）
  - **OpenAI Agents SDK / LangGraph**: `jetuse_core/agents_sdk.py` / `langgraph_engine.py` が
    `TOOLS` をラップするため基本は自動。built-in（code_interpreter等）はnative限定

## ⑤ MCPサーバーの追加（コード不要・UI操作）

- チャットのツールパネルからMCPサーバー（URL）を登録（owner分離）。認証付きはVault連携が必要
  （現状は認証なしMCPのみ有効。`docs/setup/iam.md` のVault権限が前提）

## ⑥ RAG/NL2SQLのバックエンド切替

- RAG: Vector Store（標準）/ Select AI（DB内）をUIで切替。比較は `docs/comparison/rag-backends.md`
- NL2SQL: SQL Search（標準）/ Select AI をUIで切替。比較は `docs/comparison/nl2sql-backends.md`
- 対象スキーマの変更は SemanticStore / Select AIプロファイルの再構築が必要
  （`ops/setup-sql-search.py` / `ops/setup-select-ai.py`）

## ⑦ 設定（環境変数）でのチューニング

主な `.env` / tfvars 項目（`.env.example` 参照）:

| 変数 | 効果 |
|---|---|
| `MODERATION_ENABLED` | 入力モデレーション（llama自己判定）の有効化。+0.5〜1秒/メッセージ |
| `ADMIN_USERS` | 管理ダッシュボード `/admin` を見られるユーザー（sub、カンマ区切り） |
| `rate_limit_rps` / `rate_limit_key` | API GWレート制限（送信元IP単位/全体） |
| `LOG_OCID` | OCI Loggingカスタムログへの送信先（未設定でstdoutのみ） |
| `TTS_REGION` | TTSのリージョン（既定 us-phoenix-1。日本語TTSはPhoenix限定） |
| `auth_required` / OIDC各種 | 認証の有効化と接続先Identity Domain |

## ⑧ 新しいエンドポイント/機能の追加

- 共通ロジックは `jetuse_core` に置き、CI（`service/main.py`）とFunctions（`fn/router/func.py`）の
  両方から使う（二重実装禁止 — ADR-0005）
- **SSE・プロセス内状態・6MB超アップロードはCI**、短時間・非ストリーミングはFunctions候補
  （配置の判断は `docs/comparison/compute-architecture.md`）
- 監査対象にするなら `audit.log_event(...)` を呼ぶ（機能ラベルは `source` か固定文字列）
