# ENH-02: DBチャットのテーブル内容プレビュー

実施日: 2026-06-15

## 内容
対象スキーマ(SH)の各テーブルの中身(先頭20行)を dbチャットのスキーマパネルから参照可能に。

- backend: `nl2sql.preview_table(table)` — `get_schema_info()` の既知テーブル名で検証してから
  固定識別子で `SELECT * ... FETCH FIRST 20 ROWS ONLY`(read-only `JETUSE_QUERY`)。任意SQLは受けない。
  `GET /api/dbchat/preview?table=`。
- frontend: スキーマパネルのテーブル展開時に「中身を見る(先頭20行)」ボタン→表表示。

## 検証
- ローカル実機: `preview_table("CHANNELS")` が列+5行を返却。`preview_table("EVIL; DROP TABLE X")` は
  `SqlRejectedError 未知のテーブル` で拒否(インジェクション防止)。
- build/lint/ruff グリーン。API 0.34.0 / SPA デプロイ。preview endpoint は 401(認証ゲート=アプリ稼働)。

## 備考
スキーマ/テーブルの「選択(NL2SQLスコープ反映)」と複数スキーマ対応は、CSVアップロード(ENH-01)で
ユーザー表が増えてから意味を持つため、ENH-01と合わせて拡張する。
