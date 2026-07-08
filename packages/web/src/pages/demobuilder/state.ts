/** デモビルダー(SP3-05 / specs/19 §7)の純ロジック: 型・ステップ導出・必須項目チェックリスト。
 *  UI から分離して単体テスト可能にする(ucform の流儀)。 */

export type Requirements = {
  industry?: string | null
  use_case?: string | null
  capabilities_hint?: string[] | null
  data_profile?: { documents?: string | null; tables?: string | null } | null
  notes?: string | null
}

export type PlanBlock = {
  type: string
  title: string
  system_prompt?: string
  suggested_prompts?: string[]
}
export type PlanScreen = { id: string; title: string; description?: string; blocks: PlanBlock[] }
export type PlanTable = {
  name: string
  title: string
  rows: number
  columns: { name: string; type: string; description?: string }[]
}
export type PlanDocument = { filename: string; title: string; outline: string }

export type Plan = {
  plan_version: number
  title: string
  description: string
  capabilities: string[]
  screens: PlanScreen[]
  data: { tables?: PlanTable[]; documents?: PlanDocument[] }
}

/** SessionOut(specs/19 §2.4)。demo_status は demo_id があるとき JOIN で添う */
export type Session = {
  id: string
  status: 'hearing' | 'designed'
  transcript: { role: string; content: string }[]
  requirements: Requirements | null
  plan?: Plan | null
  demo_id?: string | null
  demo_status?: string | null
  created_at: string | null
  updated_at: string | null
}

/** DemoOut(specs/18 §2.2)。config.generation は owner だけが見るサーバ管理キー */
export type Demo = {
  id: string
  name: string
  description: string | null
  status: string
  config?: { generation?: { error?: string; step?: string } }
}

export type MessageOut = {
  reply: string
  requirements: Requirements
  sufficient: boolean
  missing: string[]
}

export type Step = 1 | 2 | 3 | 4 | 5

/** サーバ状態から自然なウィザード位置を導出する(復帰と遷移の単一ロジック)。
 *  ①ヒアリング ②プラン確認 ③生成進行(failed 含む) ④プレビュー ⑤確定(ローカル遷移) */
export function deriveStep(s: Session | null): Step {
  if (!s) return 1
  if (!s.demo_id) return s.status === 'designed' && s.plan ? 2 : 1
  return s.demo_status === 'ready' ? 4 : 3
}

/** ヒアリング必須項目(specs/19 §2.2): industry / use_case / data(文書 or 表のどちらか) */
export type ChecklistItem = { key: 'industry' | 'use_case' | 'data'; ok: boolean }

const filled = (v: string | null | undefined): boolean => !!v && v.trim() !== ''

export function checklist(r: Requirements | null | undefined): ChecklistItem[] {
  return [
    { key: 'industry', ok: filled(r?.industry) },
    { key: 'use_case', ok: filled(r?.use_case) },
    {
      key: 'data',
      ok: filled(r?.data_profile?.documents) || filled(r?.data_profile?.tables),
    },
  ]
}

/** 直近セッションの復帰(specs/19 §7 — v1 は localStorage。一覧 API は residual §2.4) */
export const SID_KEY = 'jetuse.demoBuilderSid'
export const loadSid = (): string | null => localStorage.getItem(SID_KEY)
export const saveSid = (sid: string): void => localStorage.setItem(SID_KEY, sid)
export const clearSid = (): void => localStorage.removeItem(SID_KEY)
