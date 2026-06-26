# PLG-05 検証レポート — 公開フロー（builder → export → 署名 → publish）

- 日付: 2026-06-25
- ブランチ: feat/loop-engineering 派生（PLG-05 worktree）
- run-id: `runs/2026-06-25T2339_PLG-05`
- 対象 area: both（api ＋ web）／依存: PLG-01・PLG-04

## 1. 実装概要

インスタンスで作った UC/Agent 定義をマーケット（中央レジストリ PLG-04）へ公開する導線（D7=
発行者ID＋ed25519署名付き直接公開、審査キューなし）を実装した。

| 受け入れ条件 | 対応 |
|---|---|
| 既存 UC/Agent 定義を manifest 化する export | `jetuse_core/plugins/publisher.py` の `manifest_from_usecase` / `manifest_from_agent`。contributes[kind] は取込側（installer）がそのまま定義として読める形（usecase=fields/template/model、agent=instructions/model/enabledTools/framework）。表示メタ（name/description/icon/tags）は manifest トップレベル。id は `<publisher>/<name-slug>`（PLG-01 の `ID_PATTERN` に正規化、日本語のみ名は `<kind>-<id8>` にフォールバック）。 |
| 発行者鍵で manifest に署名 | `sign_manifest` が PLG-01 の `canonical_signing_payload`（signature を除く正準バイト列）を対象に ed25519 署名。鍵は `.env`/Vault（base64 32B 秘密シード）。実値はコミットしない。 |
| publish API を呼び出して公開 | `RegistryPublishClient`（HTTP）が `POST /registry/publishers/keys`（公開鍵の冪等登録）→ `POST /registry/plugins`（publish）を叩く。`publish_definition` が export→署名→鍵登録→publish を束ねる。route 層 `service/routes/plugin_publish.py` が設定欠如=503、レジストリ応答ステータス（422/409/401/403）を保って HTTP 写像。 |
| builder.tsx / agentbuilder.tsx に「マーケットに公開」アクション | 両ビルダーの編集時（既存 id）に「マーケットに公開」ボタンを追加。version を入力 → `POST /api/{usecases\|agents}/{id}/publish` → 結果/エラーを表示。i18n キー `market.publish.*`（ja/en）。 |
| builder→公開→レジストリ list に出現を実機 E2E | 後述 §3。実バケット `jetuse-registry` へ publish→list 出現を確認。 |
| `npm run build` 成功・eslint クリーン | §2。 |

新規/変更ファイルは §4。

## 2. 静的チェック

- `.venv/bin/pytest packages/api/tests` → **324 passed**（新規 `test_plugin_publisher.py` 15 件含む）。
- `.venv/bin/ruff check packages/api` → **All checks passed**。
- `npm --prefix packages/web run lint` → **クリーン**（eslint 指摘なし）。
- `npm --prefix packages/web run build` → **成功**（vite build OK。チャンクサイズ警告のみ＝既存挙動）。

## 3. 実環境 E2E（jetuse-dev / 実バケット jetuse-registry / ap-osaka-1）

実バケット `jetuse-registry`（既存・再利用、namespace は `oci os ns get`）へ、本物のコード経路で
publish→list 往復した。経路:

```
jetuse_core.plugins.publisher（export＋ed25519署名＋publishクライアント）
  → jetuse_registry.app（PLG-04 レジストリ FastAPI / in-process TestClient）
  → jetuse_registry.storage.OciObjectStore（実バケット jetuse-registry）
```

並列安全: 全オブジェクト名に固有 prefix `plg05-e2e/` を付与（index.json も `plg05-e2e/index.json`
となり PLG-06 と物理分離）。発行者も `plg05-e2e`。**E2E 後に `plg05-e2e/` 配下を全削除**。

証跡: `runs/2026-06-25T2339_PLG-05/e2e/`（`run_e2e.py` / `run_e2e.log` / `deploy.log`）。

| # | シナリオ | 期待 | 実結果 |
|---|---|---|---|
| S1 | builder UC を export→署名→publish→実バケット list 出現 | list に `plg05-e2e/plg05-e2e-faq@1.0.0`、実バケットに index.json＋成果物 | PASS（list 出現・実バケット 2 objects 確認） |
| S2 | builder Agent を export→署名→publish→list 出現 | list に Agent エントリ | PASS |
| S3 | download した manifest を発行者公開鍵で署名検証（D7 真正性） | `verify_signature == True` | PASS |
| S4 | 同一版の再 publish | 409（版は不変） | PASS |
| S5 | 無署名 manifest の publish | 422（署名必須） | PASS |
| S6 | 改ざん manifest の publish | 422（署名検証失敗） | PASS |

**TOTAL: 6/6 passed。**

後始末（独立に OCI CLI で確認）:
- `oci os object list --prefix plg05-e2e/` → 空（3 オブジェクト = index.json＋UC/Agent 成果物を削除）。
- `oci os bucket get jetuse-registry` → 存在（バケット本体は維持、新規リソース増なし）。

OCID/namespace/PAR・鍵実値・トークンは出力もコミットもしていない（発行者鍵は E2E 実行時に生成）。

### スコープ外（E2E 限定範囲）
- ブラウザ実操作（builder ボタンクリック）は自動化せず、ボタンが叩く `/publish` route 経路を
  サーバー側 publisher 経由で実バケットに対して実行した（UI→route→publisher→registry→bucket の
  publisher 以降を実環境で実証）。route 自体は `service/routes/usecases.py` / `agents.py` に追加し
  単体テストと in-process 統合テストで網羅。DB 依存の get_usecase/get_agent は単体テスト側で別途検証。

## 4. 変更ファイル（未コミット）

- `packages/api/jetuse_core/plugins/publisher.py`（新規: export／署名／publish クライアント）
- `packages/api/jetuse_core/settings.py`（発行者設定フィールド追加。実値は .env）
- `packages/api/service/schemas.py`（`PluginPublishRequest`）
- `packages/api/service/routes/plugin_publish.py`（新規: route 共通ヘルパ）
- `packages/api/service/routes/usecases.py`（`POST /api/usecases/{id}/publish`）
- `packages/api/service/routes/agents.py`（`POST /api/agents/{id}/publish`）
- `packages/api/tests/test_plugin_publisher.py`（新規: 15 件）
- `packages/web/src/pages/builder.tsx` / `agentbuilder.tsx`（「マーケットに公開」アクション）
- `packages/web/src/i18n/dict.ja.ts` / `dict.en.ts`（`market.publish.*`）
- `docs/verification/PLG-05.md`（本書）／`runs/2026-06-25T2339_PLG-05/e2e/*`（証跡）

## 5. 人間ゲート

- コミット / PR / push は未実施（承認待ち）。
- 発行者鍵・トークン・レジストリ URL は `.env`/Vault で運用者が設定（未設定時は publish=503）。
