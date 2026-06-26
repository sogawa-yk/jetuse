# PLG-06 検証レポート — マーケットプレイス UI

- タスク: PLG-06（アプリ内マーケット: 一覧・検索・タグ・詳細・install/uninstall・更新管理）
- area: web（＋ api ルート微修正）
- run-id: `runs/2026-06-25T1545_PLG-06`
- 仕様参照: `docs/enhance/202607-demo-platform-plan.md §6` / `specs/16-platform.md`
- 再利用: `packages/registry`(PLG-04) の配布契約、`jetuse_core/plugins/registry_client.py`(PLG-03 list/get/download)、
  `jetuse_core/plugins/installer.py`(PLG-03 install/uninstall)

## 受け入れ条件と結果

| 受け入れ条件 | 結果 | 証跡 |
|---|---|---|
| `/marketplace` ページ（一覧・検索・タグ・詳細）を実装 | ✅ | `packages/web/src/pages/marketplace.tsx` / UI walkthrough |
| install / uninstall ボタンが PLG-03 ロジックを API 経由で呼ぶ | ✅ | `POST /api/marketplace/install`・`/uninstall` → `installer.install/uninstall`。`e2e_marketplace.py` |
| インストール済み・更新あり（版比較）を表示 | ✅ | `build_catalog` の `installed`/`update_available`、UI の版比較ラベル `v1.0.0 → v1.2.0`。scenario-2 |
| 左ナビにマーケット導線を追加 | ✅ | `packages/web/src/components/layout.tsx`（`/marketplace`・icon `market`） |
| 一覧→詳細→install→home に出現→uninstall までUIで通る | ✅ | 下記「実機寄り E2E」+ UI walkthrough |
| `npm run build` / `npm run test` / eslint パス | ✅ | `runs/.../e2e/web_build.log`・`web_test.log`（63 passed）・eslint clean |
| api ルートを触ったため `pytest` / `ruff` パス | ✅ | `api_test.log`（321 passed）・`ruff.log`（clean） |

## 実装サマリ
- **API**: `packages/api/service/routes/marketplace.py`（`main.py` に router 登録）。
  - `GET /api/marketplace/plugins`（`q`/`tag`/`kind` 絞り込み・インストール状態/更新有無を合成）
  - `GET /api/marketplace/plugins/{namespace}/{name}`（最新 manifest 全文 + permissions + 版一覧 + 署名有無）
  - `POST /api/marketplace/install`（署名検証付き取込。例外→409/422/400/502 に正規化）
  - `POST /api/marketplace/uninstall`（取込者ゲート）
  - レジストリ通信/署名検証/取込の実体は `jetuse_core.plugins`（PLG-03）に委譲。ルートはカタログ合成と
    HTTP エラー正規化のみ。`build_client` でレジストリ未設定時は 503。
- **PLG-04 連携クライアント**: `packages/api/jetuse_core/plugins/central_registry.py`
  （`CentralRegistryClient`）。**PLG-04 が publish する実際の `index.json` 形状**
  （`objectPath`/`sha256`/発行者入れ子の `publisherKeys`）を読み、成果物の sha256 完全性検証 + 構文検証
  を行う。installer.install が要求するクライアント契約（list/download/public_key/base_url）を満たすため
  PLG-03 installer をそのまま再利用できる（PLG-03 の `RegistryClient` はモック前提の別形状のため不採用）。
- **Web**: `pages/marketplace.tsx`（一覧・検索・タグ・詳細パネル・install/uninstall・版比較）。
  純粋ヘルパ `filterPlugins`/`allTags`/`updateLabel` を単体テスト。左ナビ導線＋`market` アイコン。
  Redwood デザイントークン準拠（色のハードコードなし。`bg-cta`/`pill-err`/`action-soft` 等のトークンのみ）。
- **i18n**: `nav.market` ＋ `market.*` を ja/en に追加（キー集合一致は `dict.test.ts` で型/テスト保証）。

## 静的ゲート
- web: `npm --prefix packages/web run build` 成功（tsc -b + vite build）。`run test` 63 passed（9 files）。`eslint .` clean。
- api: `.venv/bin/pytest packages/api/tests` 321 passed（coverage 62%・閾値 45% 達成）。`ruff check packages/api` clean。

## 実機寄り E2E（完了ゲート / `runs/2026-06-25T1545_PLG-06/e2e/`）
Codex は read-only でコードを実行できないため、Claude が実コードを通して E2E を実施し証跡を残す。
タスクの並列安全制約（共有 loop dev env を deploy で同時使用しない / 実バケットは plg06-e2e/* のみ）に従い、
**OCI 実デプロイの代わりに実コードのフルスタック**を、外部 I/O のみインメモリに差し替えて End-to-End 実行した:
**実 PLG-04 `RegistryService`（実 ed25519 署名で publish）→ PLG-04 形状 index.json → `CentralRegistryClient`
（PLG-06 が使う実クライアント）→ FastAPI ルート → installer（版固定取込・署名/ sha256 検証）→ ADB 取込**。
ADB と Object Storage（`InMemoryObjectStore`）のみインメモリ。レジストリ契約は PLG-04 の本物を通す。

### シナリオ1: 一覧 → 詳細 → install → ホーム出現 → uninstall（`scenario-1.json`）
| ステップ | 期待 | 実結果 |
|---|---|---|
| `GET /api/marketplace/plugins` | acme/faq が未インストールで一覧表示 | PASS（version=1.2.0, installed=false） |
| `GET /api/marketplace/plugins/acme/faq` | permissions・署名を含む詳細 200 | PASS（permissions=[platform:rag.search], signed=true） |
| `POST /api/marketplace/install` | 署名検証して usecase を取込 200 | PASS（kind=usecase, ingested 1 件） |
| `GET /api/usecases`（ホーム） | 取込ユースケースが出現 | PASS（name=FAQ要約, source_plugin_id=acme/faq） |
| `GET /api/marketplace/plugins` | installed=true 反映 | PASS |
| `POST /api/marketplace/uninstall` | 記録・取込定義を除去 200 | PASS |
| `GET /api/usecases` | ホームから消滅・ADB 空 | PASS |

### シナリオ2: 版比較（更新あり）＋ 未署名拒否（`scenario-2.json`）
| ステップ | 期待 | 実結果 |
|---|---|---|
| 旧版 1.0.0 を install 後に一覧 | 代表版=最新 1.2.0・導入版=1.0.0・`update_available=true`（semver 比較） | PASS（1.0.0 → 1.2.0） |
| 未署名 manifest を install | 422 で拒否（fail-closed）・ADB 不変 | PASS |

### シナリオ3: 所有者ゲート uninstall（`scenario-3.json`）
| ステップ | 期待 | 実結果 |
|---|---|---|
| 別ユーザー(other-user)が install | 取込定義が ADB に存在（owner=other-user） | PASS |
| dev-user の一覧表示 | `installed=true` だが `can_uninstall=false` | PASS |
| dev-user が uninstall を試行 | 404 で拒否・他人の取込定義/記録は保持（データ破壊なし） | PASS |

合計 25 チェック PASS（`e2e_run.log`）。

### 設計判断（Codex review-1 blocker への対応）
- `installed_plugins` は (plugin_id, version) 一意 = **インスタンス単位の共有カタログ**（PLG-02）。
  よって `installed`/`update_available` はインスタンス共通の事実として表示する。
- **uninstall は取込んだ本人（`installed_by == user.subject`）だけに許可**する（他者には 404 で存在を伏せる）。
  `installer.uninstall` は出所キー (plugin_id, version) で取込定義を一括削除するため、これにより
  「任意のログインユーザーが他者の取込定義を消す」データ破壊経路を塞ぐ。スキーマ変更（owner 列追加）は
  PLG-02/03 の範疇のため本タスクでは行わず、ルート層の権限ゲートで対処（要なら別途 ADR）。
- `update_available` は **semver 比較**（最新 > 導入済み最大版）でのみ true（降格・旧版消失で誤検知しない）。
- install ボタンは `installable`（kind ∈ {usecase, agent}）のみ。sample-app 等は未対応を明示し無効化。
- uninstall は `uninstallable_versions`（viewer が取込者である版）を API が返し、UI はその版を送る
  （多版・多所有者で他人の版を送って 404 になるのを防ぐ。review-2 major への対応）。

### UI ウォークスルー（`web_test.log` / `src/pages/marketplace.ui.test.tsx`）
実 `<Marketplace/>` を描画し、一覧 → カード選択 → 詳細（permissions/署名）→ install →
「インストール済み」反映 → uninstall を**操作で**通す（1 test PASS）。

### 意図的に非実施の範囲
`runs/2026-06-25T1545_PLG-06/e2e/SKIPPED.md` 参照（共有 loop dev env への OCI 実デプロイ・実 ADB 接続・
実バケット seed は並列安全制約に基づき非実施。理由を明記。無言スキップなし）。

## PLG-03/PLG-04 index 形状の差異への対応（review-2 blocker）
- PLG-03 `RegistryClient` はモック前提の `manifest`（パス）＋ flat `publisherKeys` を読むが、実運用の
  レジストリは **PLG-04 が publish する `objectPath`/`sha256`/発行者入れ子の `publisherKeys`**。
  そのため PLG-06 は `CentralRegistryClient`（PLG-04 形状を読む新規クライアント）を追加して採用し、
  実 PLG-04 `RegistryService` が publish した本物の index で list/detail/install まで通ることを
  単体（`test_central_registry.py`）と E2E（`scenario-1..3`）で実証した。PLG-03 既存コードは変更していない。

## 残る人間ゲート
- コミット / PR / push（未実施）。本レポートと証跡を添えて人間承認を要求する。
