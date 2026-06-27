# タスク: MKT-02 中央レジストリ μService（署名・版・評価）

## ゴール
PLG-04 の MVP（Object Storage + index.json）を **ADB バックエンドの μService** へ昇格する。
評価（rating）・ダウンロード数・版ライフサイクル（active/deprecated/yanked）・DB 検索を追加し、
現行の list / get / download / publish と **後方互換**を保ち、**ed25519 署名検証**を維持する。

## 対象 area
api（packages/registry ＋ packages/api/jetuse_core/migrations）

## 依存
PLG-04（中央レジストリ Service MVP）、MKT-01（マーケット流通拡張, base=feat/stage-4）、PLG-01（manifest/署名）

## 仕様参照
docs/enhance/202607-demo-platform-plan.md §10 / docs/comparison/marketplace-plugin.md §2-B / specs/16-platform.md

## 受け入れ条件（検証可能な述語で書く）
- [ ] レジストリの保存層を差し替え可能な `RegistryBackend` 抽象に整理し、既定（`RegistryService(store, auth)`）は
      従来どおり Object Storage + index.json で動く（**後方互換**：既存 76 テストが無改変でパス）
- [ ] ADB バックエンド（`AdbBackend`）を追加：プラグイン版・発行者公開鍵・評価・DL 数・ライフサイクルを ADB に保持
- [ ] list / get / download / publish が ADB バックエンドでも従来と同じ契約で動く（成果物 sha256 検証・無署名拒否・版不変）
- [ ] ed25519 署名検証を publish 経路で維持（無署名 publish 拒否・改ざん検知）
- [ ] 評価 API：認証発行者を rater として score(1-5)＋comment を登録、集計（件数・平均）を取得（1 rater 1 件＝upsert）
- [ ] DL 数：download のたびに ADB 側でカウントを原子的に加算（index バックエンドは no-op で後方互換）
- [ ] 版ライフサイクル：所有発行者が active/deprecated/yanked を設定。latest 解決は active 優先・yanked 除外、
      yanked の明示 download は 410（Gone）、list/search は yanked 既定除外
- [ ] DB 検索：ADB バックエンドの search が SQL（kind/tag/q 部分一致）で動く
- [ ] packages/registry の単体テストが全件パス・ruff クリーン／packages/api の pytest 全件パス・ruff クリーン
- [ ] 実環境 E2E（jetuse-dev / loop ADB / 専用スキーマ `JETUSE_MKT_02` 隔離）を最低 2 本、`runs/<run-id>/e2e/` に証跡
- [ ] 証跡込み Codex レビューが PASS

## E2E シナリオ（最低 2 本・loop ADB / 専用スキーマ JETUSE_MKT_02）
1. **publish→list/get/download（署名検証＋DL カウント）**: 専用スキーマへ migrate 適用 → 発行者鍵登録 →
   署名付き manifest を publish → list/get/download が一致、download 後に DL 数が加算され、
   無署名 publish が 422、改ざん manifest が 422 で拒否されることを実 ADB 上で確認。
2. **評価＋版ライフサイクル＋DB 検索**: 同一プラグインに複数版 publish → rating 登録（集計が平均を返す）→
   旧版を deprecated・別版を yanked に遷移 → latest 解決が active を返し yanked が list/search から消え、
   yanked の明示 download が 410 → DB 検索（kind/q）が期待行を返すことを実 ADB 上で確認。
3. **後方互換（任意・追加）**: 既存 index.json バックエンド（InMemoryObjectStore 相当）の list/get/download/publish
   が無改変で従来通り動くことを単体テストで担保（E2E ではなくユニットで代替可）。

## 成果物
packages/registry/jetuse_registry/{backend,index_backend,memory_backend,adb_backend}.py /
packages/registry/jetuse_registry/{service,app,index,errors,__init__}.py（拡張）/
packages/api/jetuse_core/migrations/022_plugin_registry.sql /
packages/registry/tests/* / runs/<run-id>/e2e/* / docs/verification/MKT-02.md

## 非ゴール / 制約
- μService の実コンテナデプロイ（apply）は **人間ゲート**（エージェントは plan 止まり／E2E は loop ADB で検証し、
  実デプロイ未実施を `runs/<run-id>/e2e/SKIPPED.md` に明記）。
- 認証情報・OCID・エンドポイント実値・admin/wallet/スキーマ PW をコミットしない（証跡にも書かない）。
- コミット / PR / push は人間承認後。terraform apply はしない（plan 止まり）。
- 消費者アイデンティティ（IAM/Identity Domain 統合）の本格対応はステージ5以降。本タスクの rater は発行者トークン認証で代替。
