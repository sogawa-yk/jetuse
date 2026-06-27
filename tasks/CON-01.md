# タスク: CON-01 — コネクタ(L2 MCP)モデル＋manifest

## ゴール
`kind: connector`（L2 MCP コネクタ = Slack 等の SaaS への正規化接続）の配布表現を確定する。
manifest の `contributes["connector"]`（provider/transport/actions/auth）を構造検証し、合成
バリデーション土台（権限スコープ宣言整合）とインスタンスへの登録（DB）まで通す。**認証の実値は
一切持たない**（secret_ref = 参照名のみ。実シークレットは install 時に Vault へ束ねる＝CON-02/03）。

## 対象 area
api

## 受け入れ条件（検証可能な述語で書く）
- [x] `kind: connector` を `PluginManifest`（manifest.py の `PluginKind`）に追加する。
- [x] `jetuse_core/plugins/connector.py`: `ConnectorDefinition`（provider/transport/endpoint/auth/actions）の
      pydantic 構造検証＋`connector_json_schema()`。transport=mcp は endpoint 必須・builtin は禁止。
      endpoint はオフライン・決定的検証（https・公開ホスト literal、private/loopback IP 拒否、DNS 解決しない）。
- [x] `auth` は実シークレットを持たず `secretRef`（参照名）のみ。`kind!=none` で必須・`none` で禁止。
- [x] 合成バリデーション土台 `validate_connector_composition`（undeclared_permissions=致命 /
      unused_permissions=警告 / requires_secret・secret_ref）。fail-closed。
- [x] `jetuse_core/plugins/connector_store.py`: `register_connector`/`get_connector`/`list_connectors`/
      `remove_connector`。致命的不整合は DB に何も書かず `ConnectorCompositionError`（fail-closed）。
- [x] migration `019_connector_instances.sql`（**秘密値の列を持たない**）。
- [x] `specs/16-platform.md` に §12「kind: connector」を追記（spec 昇格）。
- [x] `.venv/bin/pytest packages/api/tests` 全件パス（正常系＋不正定義拒否＋合成＋登録往復＋fail-closed）。
- [x] `.venv/bin/ruff check packages/api` クリーン。
- [x] 既存の公開シグネチャ（manifest 検証・JSON Schema 契約）を壊さない（kind enum 追加のみ）。

## E2E シナリオ（実環境 / jetuse-dev・複数）
完了ゲートで Claude が jetuse-dev の固定 loop ADB へ migration を適用し、専用スキーマ
`JETUSE_CON_01`（schema isolation）で下記を実行して証跡を `runs/<run-id>/e2e/` に残す。最低 2 本。
Codex は実行せず、この証跡＋diff を評価する。実施不能な範囲は `runs/<run-id>/e2e/SKIPPED.md` に理由明記。
- [ ] シナリオ1（正常系・登録往復）: 専用スキーマで `019_connector_instances.sql` 適用 →
      builtin Slack コネクタ＋mcp 汎用コネクタの 2 manifest を `register_connector` →
      `connector_instances` に出現（SELECT で確認）→ `get_connector`/`list_connectors(provider=...)` で取得 →
      `remove_connector`。**定義 CLOB に実トークン値が無く secret_ref のみ**であることを SELECT で確認。
- [ ] シナリオ2（異常系・fail-closed）: action が要求する Platform スコープを manifest.permissions が
      宣言していない manifest を `register_connector` → `ConnectorCompositionError` で拒否され
      `connector_instances` に行が増えないこと（DB 件数 before==after）を確認。
- [ ] 冪等性: migration の再適用が no-op（schema_migrations で既適用スキップ）であることを確認。

## 非ゴール / 制約
- 実コネクタ本体（Slack 実装・MCP 呼び出し／Responses API type:"mcp"）は CON-02。
- 合成（sample-app × AI 部品 × connector）への組込＋本格 E2E は CON-03。
- 実シークレットの Vault 束ね・install フローは本タスク非ゴール（CON-02/03）。
- 既存リソース（VCN develop / インスタンス dev / バケット）は参照のみ。jetuse-dev の loop ADB を再利用。
- spec-driven: specs/ にない判断は実装せず docs/decisions/ に ADR 案を書く。認証実値はコミットしない。
