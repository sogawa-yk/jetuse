/** RAG バックエンドの語彙。
 *
 *  以前はここに能力差(RAGM-03 / ADR-0020 §3)の型と取り出しも置き、チャット画面へ
 *  「このバックエンドで何ができるか」の表を出していた。**利用者には邪魔だった**ため
 *  2026-08-20 に画面から外した(利用者指摘)。事実そのものは API の
 *  `GET /api/capabilities` が引き続き持っている。
 */

/** チャットで選べる RAG バックエンド(API 側 ChatRequest.rag_backend と同じ集合)。 */
export type RagBackend = 'vector_store' | 'adb' | 'select_ai' | 'opensearch'

/** 取り込み状況(そのファイルを取り込めたか)。能力差(何ができるか)とは別物。 */
export type BackendStatus = 'indexed' | 'pending' | 'error' | 'disabled'

