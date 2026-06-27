# タスク: ASSET-01 — 既存資産オンボード（伝ぴょん / No.1-RAG / No.1-SQL-Assist）

## ゴール
既存の外部資産を JetUse プラットフォームの配布表現へ正規化してオンボードする。

1. **No.1-RAG / No.1-SQL-Assist**: 検索 / NL2SQL パイプラインを **L2 MCP コネクタ（kind: connector）**
   として正規化する manifest＋接続定義を確定する（Gradio UI は捨てる。認証は **Vault OCID 参照のみ**
   ＝ secretRef、実シークレット値は持たない）。
2. **伝ぴょん**: 外部アプリ連携（iframe / link 埋め込み＋ **OIDC SSO ブリッジ**）の配布表現
   （**kind: external-app**）の設計＋最小実装（SSO ブリッジ受け渡しの決定的・オフライン土台）。
3. オンボード方式（外部連携 or MCP 化・必要権限・データ境界）を `docs/verification/ASSET-01.md`
   に整理し、spec（specs/16-platform.md §14）へ昇格する。

**既存外部資産は参照のみ・改変しない。terraform apply はしない。コミット / PR / push はしない。**

## 対象 area
api（＋docs）

## 受け入れ条件（検証可能な述語で書く）
- [ ] `jetuse_core/plugins/asset_connectors.py`: No.1-RAG（action `search` / `platform:rag.search`）と
      No.1-SQL-Assist（action `nl2sql` / `platform:db.query`）の **transport=mcp** コネクタを
      CON-01 の `ConnectorDefinition` で構造化する builder（endpoint・secretRef を引数化、実値を持たない）。
      合成バリデーション（`validate_connector_composition`）が ok=True を返す（宣言整合）。
- [ ] `kind: external-app` を `PluginKind` に追加し、`jetuse_core/plugins/external_app.py` に
      `ExternalAppDefinition`（embed=iframe|link / url / OIDC SSO ブリッジ＝issuer・clientIdRef・
      secretRef・audience・scopes・claimMapping）の pydantic 構造検証＋`external_app_json_schema()`＋
      `register_contributes_validator("external-app", ...)` を実装する。**実シークレットを持たない**
      （clientIdRef / secretRef = 参照名のみ。url は https・公開ホスト literal・private/loopback 拒否）。
- [ ] `external_app.py` に **SSO ブリッジ最小実装** `build_sso_handoff(definition, subject)` を実装する。
      決定的・オフライン（IdP へ実通信しない）で OIDC authorize ハンドオフ／token-exchange 要求の
      shape を組み立て、**実シークレット値・実トークンを一切含まない**（参照名のみ）。fail-closed。
- [ ] 伝ぴょん（denpyon）の external-app manifest builder を提供する。
- [ ] `specs/16-platform.md` に §14「kind: external-app（外部アプリ連携 / SSO ブリッジ）」を追記（spec 昇格）。
- [ ] `.venv/bin/pytest packages/api/tests` 全件パス（正常系＋不正定義拒否＋合成＋SSO ハンドオフ＋
      実値混入拒否＋既存テスト非回帰）。
- [ ] `.venv/bin/ruff check packages/api` クリーン。
- [ ] 既存の公開シグネチャ（manifest 検証・JSON Schema 契約・connector API）を壊さない
      （kind enum 追加のみ＝additive）。

## E2E シナリオ（実環境 / jetuse-dev・複数。最低 2 本）
完了ゲートで Claude が実施し、証跡を `runs/<run-id>/e2e/` に残す。専用スキーマ `JETUSE_ASSET-01`
（schema isolation）。Codex は実行せず証跡＋diff を評価する。実施不能な範囲は SKIPPED.md に理由明記。
- [ ] シナリオ1（mock MCP コネクタ疎通・No.1-RAG / No.1-SQL-Assist）: `invoke_connector_action` に
      **mock mcp_caller** を注入し、broker 認可（`platform:rag.search` / `platform:db.query` ＋
      `platform:connector.invoke`）を通して search / nl2sql を1回ずつ呼び出し、戻り値が得られること、
      **実トークンが戻り値・監査・例外に出ない**ことを確認する。
- [ ] シナリオ2（伝ぴょん SSO ブリッジ mock 検証）: `build_sso_handoff` で OIDC ハンドオフ／
      token-exchange shape を生成し、**実シークレット値・実トークンが含まれない**（参照名のみ）こと、
      欠落クレーム等で fail-closed になることを確認する。
- [ ] （任意・DB 到達可能なら）シナリオ3: 専用スキーマで `019_connector_instances.sql` 適用 →
      asset コネクタ 2 manifest を `register_connector` → `connector_instances` に出現（SELECT）→
      定義 CLOB に実トークンが無く secretRef のみであることを確認 → `remove_connector`。

## 非ゴール / 制約（人間ゲート）
- **実資産接続・実 MCP エンドポイント配備・実 Vault 束ね・実 OIDC IdP 設定（client_secret 投入）は
  人間ゲート**（SKIPPED.md 明記）。本タスクは mock 検証まで。
- 合成（sample-app × AI 部品 × connector × external-app）への本格組込は後段。
- 既存リソース（VCN develop / インスタンス dev / バケット）は参照のみ。jetuse-dev の loop ADB を再利用。
- spec-driven: specs/ にない判断は実装せず docs/decisions/ に ADR 案を書く。認証実値はコミットしない。
