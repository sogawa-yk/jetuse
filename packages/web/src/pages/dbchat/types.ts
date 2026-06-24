/** DBチャットの共有型(dbchat.tsx分割: review-validation.md §7)。 */
export type Result = { columns: string[]; rows: string[][]; row_count: number; truncated: boolean }

export type SchemaTable = {
  name: string
  comment: string
  rows: number | null
  columns: { name: string; type: string; comment: string }[]
}

export type Dataset = { id: string; display_name: string; columns: string[]; row_count: number }
