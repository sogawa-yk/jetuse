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

## ⑨ 別テナンシ / リージョンへの持ち出し（可搬性・ORMスタック変数）

ワンクリックスタック（`infra/orm`）は自環境固有値を持たないよう可搬化してある（PORT-01）。
別テナンシへデプロイする前に README の「デプロイ前チェックリスト」を確認し、必要に応じて次の変数で調整する。

| 変数 | 既定 | いつ変える |
|---|---|---|
| `allow_unvalidated_genai_region` | false | GenAI実証済（大阪/シカゴ）以外へ承知の上でデプロイするとき（plan時にエラーで停止する） |
| `adb_ecpu_count` / `adb_db_version` | 2 / 26ai | ADB ECPU枠が足りない、または 26ai 非提供のリージョン |
| `ci_shape` | CI.Standard.E4.Flex | E4.Flex が提供されないリージョン |
| `ocir_namespace` | 公開namespace | イメージを自テナンシへミラーした場合のみ（自テナンシの Object Storage namespace とは無関係） |
| `semstore_ocid` | 空 | NL2SQL(SQL Search)を使うとき（事前作成した Semantic Store の OCID。空だと503） |

- **SPA配信PARの期限**: ORM 利用者は入力不要で、既定で apply 時刻起点+1年の相対期限になる（基準時刻を
  `time_offset` リソースが state に固定するため plan 毎の差分は出ない）。**これは ORM スタック変数ではない** ——
  固定の絶対日付を使いたい場合は object-storage / spa-bucket モジュールを直接利用する側で `spa_par_expiry` に
  RFC3339 を渡す（module 変数。指定時はその値を尊重し後からの変更も反映）。**固定日付から相対期限へ移行する既存
  スタックは、初回 apply で PAR が1回だけ再発行される**（access_uri が変わるが Terraform が API Gateway バックエンドへ再配線する）。
- **Identity Domain の destroy（`enable_auth=true` 時のみ）**: ドメインは ACTIVE のままだと削除できないため、
  スタックの destroy 時に provisioner がテナンシのホームリージョンで自動 deactivate する。RM ランナーで CLI 認証が
  効かず deactivate に失敗した場合は、**手動で**ホームリージョンにて `oci iam domain deactivate --domain-id <id>
  --region <home>` を実行してから再度 destroy する（未 deactivate のまま同 prefix で再デプロイすると衝突する）。
