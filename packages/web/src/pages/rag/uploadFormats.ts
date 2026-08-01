/** アップロードで選べる文書の形式。
 *
 *  API 側の受け口（`jetuse_core.rag.ALLOWED_EXTENSIONS` = pdf / txt / md / xlsx /
 *  png / jpg / jpeg）と揃える。
 *  ここが狭いと「サーバは受け付けるのに画面から選べない」状態になる（RAGM03-002 の指摘。
 *  xlsx はチャンクごとのセル範囲出典の主役なのに、ファイル選択で出てこなかった）。
 *  画像とスキャン PDF は OCR を通して取り込む（PREP-03）。
 */
export const UPLOAD_EXTENSIONS = ['.pdf', '.txt', '.md', '.xlsx', '.png', '.jpg', '.jpeg'] as const

/** <input type="file" accept> に渡す文字列。 */
export const UPLOAD_ACCEPT = UPLOAD_EXTENSIONS.join(',')
