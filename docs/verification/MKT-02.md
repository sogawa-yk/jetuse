# 検証レポート: MKT-02 中央レジストリ μService（署名・版・評価）

- run: `runs/2026-06-27T1048_MKT-02/`
- base: `feat/stage-4`（MKT-01 のマーケット流通拡張を含む）
- 対象: `packages/registry`（昇格）＋ `packages/api/jetuse_core/migrations/022_plugin_registry.sql`（新規）

## 概要
PLG-04 の MVP（Object Storage + index.json）を **ADB バックエンドの μService** へ昇格した。保存層を
差し替え可能な `RegistryBackend` 抽象に整理し、評価・ダウンロード数・版ライフサイクル（active/deprecated/
yanked）・DB 検索を追加。現行の list/get/download/publish と後方互換を保ち、ed25519 署名検証を維持した。

### 主要設計
- **backend 抽象**: `backend.RegistryBackend`（Protocol）。実装は 3 種:
  - `IndexBackend`（レガシー Object Storage + index.json。後方互換。拡張操作は `RegistryUnsupportedError`=501）
  - `InMemoryRegistryBackend`（全機能インメモリ。service ロジック・拡張の単体テスト用）
  - `AdbBackend`（ADB μService 本体。版/鍵/評価/DL 数/ライフサイクルを ADB に保持、成果物を行内 CLOB に格納し ADB 自己完結）
- **後方互換**: `RegistryService(store, auth)` は従来どおり IndexBackend で動作（既存 76 テストは内部参照の
  軽微な追従（`_store`→`_backend._store`）のみで挙動不変。新規 24 テストを追加）。
- **署名検証**: publish の認証・manifest 検証・publisher 一致・無署名拒否・ed25519 検証は service 層に集約し
  バックエンド非依存に維持（PLG-01 `verify_signature` 再利用）。
- **原子性**: 版不変=(plugin_id,version) PK / DL 数=`UPDATE ... +1 RETURNING`（行ロック）/ 評価=MERGE upsert。
- **ライフサイクル**: latest 解決は active 優先・deprecated フォールバック・yanked 除外。yanked の明示取得は 410。
  list/search は yanked を既定除外（`includeYanked` で監査表示）。変更は所有発行者のみ（403）。

## 静的検証（コミット前チェック）
- `packages/registry`: **ruff クリーン** / **pytest 100 passed**（既存 76 ＋ 新規 24）。
- `packages/api`: **ruff クリーン** / **pytest 814 passed**（migration 022 を含む `test_migrate_idempotent` 緑）。
  - 注: `packages/api` の一部テストは `jetuse_registry` を import するため `PYTHONPATH=packages/registry` で実行
    （editable 衝突回避。memory: loop-e2e-jetuse-core-editable-gotcha）。

## 実環境 E2E（jetuse-dev / loop ADB / 専用スキーマ JETUSE_MKT_02）
証跡: `runs/2026-06-27T1048_MKT-02/e2e/`（deploy.log / scenario-1.json / scenario-2.json /
scripts/ / SKIPPED.md / APPROVAL.md）。接続実値・パスワードは非コミット（env 注入）。

- **デプロイ**: `jetuse-loop-adb` の専用スキーマ JETUSE_MKT_02 へ全 22 migration を適用。022_plugin_registry で
  `REGISTRY_PUBLISHER_KEYS / REGISTRY_PLUGINS / REGISTRY_RATINGS` を作成。**冪等再適用が no-op** を確認。
- **シナリオ1（publish/list/get/download＋署名＋DL カウント）= PASS**:
  鍵登録→署名 publish→list/get/download が一致、download 2 回で DL 数 1→2 加算、**無署名 publish 422**・
  **改ざん manifest 422**・**再 publish 409（版不変）** を実 ADB 上で確認。
- **シナリオ2（評価＋版ライフサイクル＋DB 検索＋410）= PASS**:
  2 版＋別 kind を publish→2 rater 評価（平均 3.0）→同 rater upsert（件数不変・平均 3.5）→2.0.0 を deprecated に
  すると latest が 1.0.0 へ、1.0.0 を yank すると list から消え latest が 2.0.0 にフォールバック・**yanked の明示
  download が 410**・`includeYanked` で監査表示、**DB 検索**（kind=agent / q=faq / tag=sales）が期待 id を返す。

## 残る人間ゲート
- μService の実コンテナデプロイ（apply・課金）。本タスクは loop ADB での実機検証まで（SKIPPED.md 明記）。
- terraform apply 全般（plan 止まり。本タスクはインフラ変更なし）。
- コミット / PR / push。
