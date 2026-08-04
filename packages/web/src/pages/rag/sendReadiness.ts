/** 送信可否（RAGM-04）: 「選択中のバックエンドに取り込めたファイルが 1 つでもあるか」。
 *
 *  以前はマネージド側の取り込み状態（`status === 'completed'`）だけで決めていたため、
 *  ADB を選んでいてもマネージド側が完了していれば送信できるように見えていた（RAGM03-005）。
 *  判定根拠を「選択中のバックエンドの取り込み状態」へ正すだけで、
 *  「取り込めたか（バッジ）」と「何ができるか（能力表示）」の区別は変えていない。
 */
import type { BackendStatus, RagBackend } from './capabilityCatalog'

type FileIngestState = {
  status: 'processing' | 'completed' | 'failed'
  backends?: Partial<Record<RagBackend, BackendStatus>>
}

/** そのファイルを、選択中のバックエンドへの質問に使えるか。 */
export function isSendableWith(file: FileIngestState, backend: RagBackend): boolean {
  // `backends` を持たない応答（前方互換 / 縮退時）から分かるのはマネージド側の状態だけ。
  // 他のバックエンドを選んでいるときにそれで代用すると、直そうとしているズレが戻る。
  if (!file.backends) return backend === 'vector_store' && file.status === 'completed'
  return file.backends[backend] === 'indexed'
}

/** 選択中のバックエンドで質問できる状態か（＝送信可否）。 */
export function hasSendableFile(files: FileIngestState[], backend: RagBackend): boolean {
  return files.some((f) => isSendableWith(f, backend))
}
