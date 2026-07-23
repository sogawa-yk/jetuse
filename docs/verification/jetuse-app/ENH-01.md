# ENH-01: 構造化データ(CSV)アップロード→DBチャット対象化

実施日: 2026-06-15 / SPIKE-ENH01=PASS(アプリ直結ロード方式)

## 方式(comparison: アプリ直結ロード採用)
オブジェクトストレージ/DBMS_CLOUDを使わず、APIでCSVを解析し ADB に直接ロード:
1. CSV解析(ヘッダ+行)、列名サニタイズ・型推論(全数値→NUMBER 否→VARCHAR2)
2. `JETUSE_APP` に CREATE TABLE → executemany 投入 → `JETUSE_QUERY` に SELECT付与
3. 本人専用 Select AI プロファイル(`JETUSE_DS_<ownerハッシュ>`, object_list=本人のデータセット表)を再構築
4. メタデータ表 `JETUSE_DATASETS`(owner_sub/table/display/columns/rows)で一覧・削除管理
- NL2SQLは `target=datasets` のとき本人プロファイルでSQL生成し、`JETUSE_QUERY`(読取専用)で実行。
- 追加のIAM/オブジェクトストレージ不要(=マネージドDB機能内で完結)。

## 検証(ローカル実機 SMOKE PASS)
- `create_dataset`(CSV)→ 表作成+3行投入+GRANT+プロファイル再構築。
- `generate_sql_select_ai(profile=本人)` → `SELECT region, SUM(amount) ... GROUP BY region` 生成。
- `execute_readonly` → 集計結果 East=180/West=250。`preview` → サンプル行。`delete_dataset` → 表/プロファイル/メタ削除。
- SPIKE単体: CREATE+load+GRANT→Select AI showsql→read-only exec を別途確認(SPIKE-ENH01)。

## API / UI
- `POST/GET/DELETE /api/db/datasets`、`GET /api/db/datasets/{id}/preview`。`POST /api/chat/nl2sql` に `target`。
- dbchat: 「対象データ」トグル(サンプルDB(SH) / マイデータ(CSV))、CSVアップロード・一覧・プレビュー・削除。
- API 0.35.0 / SPAデプロイ。datasets endpointは401(認証ゲート=稼働)。

## 残(ENH-02/03の延長)
- スキーマ/テーブルの明示選択でのNL2SQLスコープ細粒度制御、プロファイル/モデル選択の露出は今後の小改修。
