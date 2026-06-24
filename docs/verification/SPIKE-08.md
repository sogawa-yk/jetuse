# SPIKE-08: Select AI RAG（DBMS_CLOUD_AIベクトル索引 + narrate）検証

実施日: 2026-06-11 / 実行: `spikes/spike08_select_ai_rag.py` / 対象: jetuse-spike-adb23（23ai、検証後STOPPED）

## 目的

RAG-03（バックエンド切替）の前提検証。バケット文書→ADB内ベクトル索引→`SELECT AI narrate` のRAGが大阪リージョンで動くか、品質・レイテンシをFile Search方式（RAG-01/02）と同一条件で比較する。

## 重要な発見（実機確定）

1. **Select AIのベクトル索引はADB 23ai必須**: jetusedev（19c）では `ORA-20047: Oracle vector index is not support in current database version`。**現行のjetusedevでは動かない** — RAG-03実装の前提条件
2. 23aiでは一発で動作: credential（SPIKE-04パターン）→ profile（`embedding_model: cohere.embed-multilingual-v3.0` + `vector_index_name`）→ `CREATE_VECTOR_INDEX`（`vector_db_provider: oracle`、バケットURL直指定）
3. 索引構築は3文書（PDF/txt/md）で**約20秒**（`{INDEX}$VECTAB` の行数で進捗確認可能）
4. 応答末尾に **Sources（ファイル名+オブジェクトURL）が自動付与**される
5. oracledbで `fetch_lobs=False` 必須（GENERATEの戻りはCLOB）

## 計測結果（日本語10問、SPIKE-03質問セット）

| 指標 | Select AI RAG（本SPIKE） | File Search（RAG-02、アプリ経由） |
|---|---|---|
| キーワード正答 | **10/10** | 9/10（1件は「50キロ/50km」表記揺れ） |
| 正引用 | 10/10（Sources） | 10/10（annotations+スコア） |
| レイテンシ中央値 | **1.6s**（範囲1.3-2.6s） | 4.3s（範囲3.2-5.5s、gpt-oss推論込み） |

詳細比較・採用判断: **docs/comparison/rag-backends.md（RAG-04）**

## RAG-03への示唆

- 切替実装自体は小さい（`rag_backend=select_ai` でGENERATEをラップ、非ストリーミング→単発delta+Sources解析）
- **ブロッカー: jetusedevの23ai化が必要**（アップグレード or 再作成 — データ移行とダウンタイムを伴うため人間判断）
- 23ai化するならPhase 5（NL2SQL）とのDB内統合という大きい果実がある
