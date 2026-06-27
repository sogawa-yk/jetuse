# ASSET-01 検証レポート: 既存資産オンボード（伝ぴょん / No.1-RAG / No.1-SQL-Assist）

日付: 2026-06-27
仕様: specs/16-platform.md §12（connector）/ §14（external-app）
状態: **実装＋単体テスト完了**（Codex レビュー＋実環境 E2E は完了ゲートで実施）

## 1. 目的とスコープ

独自フロント（Gradio UI 等）を持つ既存資産を JetUse プラットフォームの**配布表現**へ正規化して
オンボードする。**既存外部資産は参照のみ・改変しない**。実資産接続・実 Vault 束ね・実 SSO 設定は
人間ゲート（本タスクは mock 検証まで）。

| 資産 | 性質 | 採用オンボード方式 |
|---|---|---|
| No.1-RAG | 検索パイプライン（API 化可能） | **L2 MCP コネクタ（kind: connector）** |
| No.1-SQL-Assist | NL2SQL パイプライン（API 化可能） | **L2 MCP コネクタ（kind: connector）** |
| 伝ぴょん | 独自フロント UI 完結型アプリ | **外部アプリ連携（kind: external-app / iframe＋OIDC SSO）** |

## 2. オンボード方式比較（外部連携 vs MCP 化）

既存資産を JetUse に載せる選択肢は大きく 2 つ。**資産が「呼び出すべき API パイプライン」か「使わせる
べき画面」か**で選ぶ。

| 観点 | MCP 化（kind: connector / §12） | 外部連携（kind: external-app / §14） |
|---|---|---|
| 何を載せるか | 検索 / NL2SQL 等の**パイプライン（API）**。UI は捨てる。 | 外部アプリの**画面そのもの**（独自フロント）。 |
| 統合点 | JetUse のエージェント/合成から **action を invoke** して結果を使う（プログラム的合成可）。 | JetUse 画面に **iframe / link で埋め込み**、利用者が直接操作。 |
| 認可・監査 | Platform API ブローカー（PAPI-01）が**毎 invoke を認可・監査**（fail-closed）。 | OIDC SSO で**身元を引き渡す**（認可はアプリ側）。JetUse は SSO ブリッジまで。 |
| データ境界 | action が要求する **Platform スコープ**で表現（rag.search / db.query）。テナント境界は broker token の tenant claim。 | アプリが独自に持つデータ境界。JetUse は claimMapping で渡す身元属性のみ管理。 |
| 必要権限 | `platform:connector.invoke` ＋ action 宣言スコープ。secret は Vault secretRef。 | OIDC client（clientIdRef/secretRef は Vault）。Platform スコープ不要。 |
| 適する資産 | API 化でき、AI 合成に組み込みたいパイプライン資産。 | UI 完結・短期オンボード優先・改修したくない既存アプリ。 |
| 実装コスト | 中（manifest＋接続定義。実 MCP エンドポイント配備が必要）。 | 小（iframe＋SSO 設定。アプリ本体は無改修）。 |

**判断**: No.1-RAG / No.1-SQL-Assist は「検索 / NL2SQL という**呼び出すパイプライン**」であり AI 合成に
組み込む価値が高い → **MCP 化**。伝ぴょんは「独自フロントの**画面**を使わせる」UI 完結型で、無改修・短期
オンボードを優先 → **外部連携（iframe＋OIDC SSO）**。

## 3. No.1-RAG / No.1-SQL-Assist（MCP コネクタ化）

実装: `packages/api/jetuse_core/plugins/asset_connectors.py`（CON-01 `ConnectorDefinition` を再利用）。

- **transport=mcp**: 各資産は外部にデプロイされた MCP サーバー（HTTPS）。endpoint は環境依存のため
  builder 引数（manifest にハードコードしない）。invoke は CON-02 `connector_runtime` の mcp 経路
  （Responses API type:"mcp"）を再利用。
- **認証は Vault OCID 参照のみ**: `auth.kind=api_token`・`secretRef`（論理参照名）のみ。**実 API トークンは
  配布表現・DB・証跡のいずれにも持たない**。実値は install 時に Vault へ束ねる（人間ゲート）。
- **データ境界＝Platform スコープ**:

| 資産 | provider | action | 要求 Platform スコープ | secretRef（参照名） |
|---|---|---|---|---|
| No.1-RAG | `no1-rag` | `search` | `platform:rag.search` | `no1-rag-api-token` |
| No.1-SQL-Assist | `no1-sql-assist` | `nl2sql` | `platform:db.query` | `no1-sql-assist-api-token` |

合成バリデーション（宣言整合）は両資産で ok=True（undeclared_permissions 空）。invoke 時は
`platform:connector.invoke` ＋ 上記スコープを broker が強制し、拒否時は外部 MCP へ到達しない（fail-closed）。
解決済みトークンは戻り値・例外・監査・ログに出さない（CON-02 の redact 契約を継承）。

## 4. 伝ぴょん（外部アプリ連携 / OIDC SSO ブリッジ）

実装: `packages/api/jetuse_core/plugins/external_app.py`（kind フレームワーク）＋
`denpyon_external_app.py`（伝ぴょん builder）。

- **embed=iframe＋url**（伝ぴょんの HTTPS エンドポイント。環境依存のため builder 引数）。url はオフライン
  検証（https・公開ホスト・private/loopback 拒否・認証値埋め込み禁止）。
- **OIDC SSO ブリッジ**: issuer（IdP）・`clientIdRef`・`secretRef`（client_secret＝Vault）・audience・
  scopes（openid 必須）・claimMapping。**実 client_secret / 実トークンは持たない**（参照名のみ）。
- **claimMapping**（JetUse 身元 → 伝ぴょん側クレーム）: `sub→preferred_username` / `email→email` /
  `groups→roles`。
- **SSO ブリッジ最小実装** `build_sso_handoff`: 決定的・オフラインで **RFC 8693 token-exchange 要求の shape**
  ＋ claimMapping 適用済みクレームを組み立てる。client_id/client_secret/subject_token は参照名のみ。
  claim 欠落・sso 未宣言・state/nonce 欠落は fail-closed。`contains_secret_values=False` を不変条件として返す。

## 5. データ境界の整理

- **コネクタ（No.1-RAG/SQL-Assist）**: テナント境界は broker token の tenant claim（Project OCID）。資産が
  触れるデータドメインは Platform スコープ（rag.search / db.query）で明示し、毎 invoke を監査
  （`platform_broker_audit`）。実シークレットは Vault のみ（配布物・DB に出ない）。
- **external-app（伝ぴょん）**: JetUse が管理するのは「埋め込み URL」と「SSO で渡す身元属性（claimMapping）」
  のみ。アプリ内のデータ・認可はアプリ側の責務。client_secret は Vault（参照名のみ JetUse 側に保持）。

## 6. 必要権限（人間ゲート整理）

| 項目 | 種別 | 状態 |
|---|---|---|
| No.1-RAG/SQL-Assist の実 MCP エンドポイント配備 | 実資産接続 | 人間ゲート（未実施） |
| 上記 API トークンの Vault 束ね（secretRef → 実値） | Vault / 認証実値 | 人間ゲート（未実施） |
| 伝ぴょんの OIDC client 登録（client_id/secret） | IdP / 認証実値 | 人間ゲート（未実施） |
| 伝ぴょんの実 iframe 埋め込み URL・実 SSO 設定 | 実資産接続・SSO 実設定 | 人間ゲート（未実施） |
| Platform スコープ承認（テナントへの grant） | PAPI-02 承認フロー | 運用時（テナント管理者） |

## 7. 検証（単体）

- `.venv/bin/pytest packages/api/tests`: **990 passed**（既存非回帰＋ASSET-01 新規）。
- `.venv/bin/ruff check packages/api`: **All checks passed**。
- 新規テスト: `test_asset_connectors.py`（定義/合成/mock MCP 疎通/secret 非漏洩/fail-closed）、
  `test_external_app.py`（定義検証/不正拒否/manifest round-trip/SSO ハンドオフ/fail-closed/伝ぴょん builder）。
- 既存契約非回帰: kind enum に `external-app` を additive 追加（`test_plugin_manifest` の JSON Schema 期待値更新）。

## 8. 実環境 E2E（完了ゲート）

→ `runs/<run-id>/e2e/` を参照（シナリオ・実行コマンド・実結果・SKIPPED 理由）。

**実施できたもの（scenario-1 / scenario-2 = PASS）**: 実環境にデプロイ済みの実コード（jetuse_core
editable / 同一 venv）に対し、**DB を介さない in-process** の end-to-end を実行した。scenario-1 は mock
mcp_caller で No.1-RAG search / No.1-SQL-Assist nl2sql を broker 認可（rag.search / db.query ＋
connector.invoke）越しに疎通し、実トークン非漏洩（戻り値＋監査の構造的非漏洩）と scope 欠落の fail-closed を
確認。scenario-2 は伝ぴょん SSO ハンドオフ（claimMapping 適用・参照名のみ・fail-closed）を確認。

**実施できなかったもの（SKIPPED.md 参照）**: 本隔離 worktree に `.env`（loop ADB 資格情報）が無いため、
**専用スキーマ `JETUSE_ASSET-01` での DB 登録往復（connector_instances）と audit 行の SELECT は未実施**
（SKIPPED.md 1 / 1b に理由と再実行コマンドを明記）。register_connector 経路は CON-01 の実環境 E2E で検証済みで、
ASSET-01 の新規ロジックは scenario-1/2 で網羅。実資産接続・実 MCP エンドポイント・実 Vault 束ね・実 OIDC
IdP 設定は人間ゲート（SKIPPED.md 2）。
