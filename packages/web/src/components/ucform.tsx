/** ユースケースの動的フォームレンダラ(UC-01)。実行ページとビルダープレビューで共用 */
/* eslint-disable react-refresh/only-export-components -- 型・ヘルパーとレンダラの同居ファイル */

export type UcField = {
  name: string
  label: string
  type?: 'text' | 'textarea' | 'select' | 'number' | 'url'
  required?: boolean
  placeholder?: string | null
  options?: string[] | null
  default?: string | null
}

export type UcDefinition = {
  id?: string
  name: string
  description?: string | null
  icon?: string | null
  tags?: string[]
  model?: string | null
  visibility?: 'private' | 'public'
  builtin?: boolean
  owner_sub?: string | null
  mine?: boolean
  fields: UcField[]
  template: string
}

/** {{var}} を値で置換。未定義変数は空文字(specs/08) */
export function renderTemplate(template: string, values: Record<string, string>): string {
  return template.replace(/\{\{(\w+)\}\}/g, (_, name: string) => values[name] ?? '')
}

export function initialValues(fields: UcField[]): Record<string, string> {
  return Object.fromEntries(fields.map((f) => [f.name, f.default ?? '']))
}

export function missingRequired(fields: UcField[], values: Record<string, string>): string[] {
  return fields.filter((f) => f.required && !(values[f.name] ?? '').trim()).map((f) => f.label)
}

const inputCls =
  'w-full rounded-rw border border-line bg-surface px-3 py-2 text-sm outline-none focus:border-action'

export function UcForm({
  fields,
  values,
  onChange,
  disabled,
}: {
  fields: UcField[]
  values: Record<string, string>
  onChange: (name: string, value: string) => void
  disabled?: boolean
}) {
  return (
    <div className="space-y-3">
      {fields.map((f) => (
        <label key={f.name} className="block">
          <span className="mb-1 block text-sm font-medium">
            {f.label}
            {f.required && <span className="ml-1 text-primary-strong">*</span>}
          </span>
          {f.type === 'textarea' ? (
            <textarea
              rows={6}
              value={values[f.name] ?? ''}
              onChange={(e) => onChange(f.name, e.target.value)}
              placeholder={f.placeholder ?? ''}
              disabled={disabled}
              className={`${inputCls} resize-y`}
            />
          ) : f.type === 'select' ? (
            <select
              value={values[f.name] ?? f.default ?? ''}
              onChange={(e) => onChange(f.name, e.target.value)}
              disabled={disabled}
              className={inputCls}
            >
              {(f.options ?? []).map((o) => (
                <option key={o} value={o}>
                  {o}
                </option>
              ))}
            </select>
          ) : (
            <input
              type={f.type === 'number' ? 'number' : f.type === 'url' ? 'url' : 'text'}
              value={values[f.name] ?? ''}
              onChange={(e) => onChange(f.name, e.target.value)}
              placeholder={f.placeholder ?? (f.type === 'url' ? 'https://' : '')}
              disabled={disabled}
              className={inputCls}
            />
          )}
        </label>
      ))}
    </div>
  )
}
