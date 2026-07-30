# 片付け（この run が作った資源のみ・不在は NotFound で確認）

- DELETE /api/rag/files/<id>（inventory-api-spec-v1.md） -> 200 / Files API 再照会で NotFound: True
- DELETE /api/rag/files/<id>（inventory-api-spec-v2.md） -> 200 / Files API 再照会で NotFound: True
- 登録簿の残ファイル: 0 件
- Vector Store `jetuse-spike-ragm01-d6ea37` 削除 -> NotFound: True
- 登録簿(JETUSE_RAGM01): rag_stores 0 行 / rag_files 0 行

## 検証用スキーマの片付け（receipt の user_id を照合して DROP）

- JETUSE_RAGM01: DROP 実行 → 再照会 0 件（0 なら削除済み）
- JETUSE_RAGM01_Q: DROP 実行 → 再照会 0 件（0 なら削除済み）
