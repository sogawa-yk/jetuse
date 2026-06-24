import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import {
  UcForm,
  renderTemplate,
  initialValues,
  missingRequired,
  type UcField,
} from './ucform'

/** ユースケース動的フォーム(UC-01)のテンプレート置換・必須検証・レンダリング(review-validation.md §7)。 */
describe('ucform helpers', () => {
  describe('renderTemplate', () => {
    it('substitutes {{var}} with values', () => {
      expect(renderTemplate('要約: {{text}} ({{length}})', { text: '本文', length: '短く' })).toBe(
        '要約: 本文 (短く)',
      )
    })

    it('replaces undefined variables with empty string', () => {
      expect(renderTemplate('a={{a}} b={{b}}', { a: 'X' })).toBe('a=X b=')
    })

    it('replaces all occurrences of the same variable', () => {
      expect(renderTemplate('{{x}}-{{x}}', { x: '1' })).toBe('1-1')
    })

    it('leaves non-matching braces untouched', () => {
      expect(renderTemplate('{ literal } {{v}}', { v: 'ok' })).toBe('{ literal } ok')
    })
  })

  describe('initialValues', () => {
    it('seeds each field with its default or empty string', () => {
      const fields: UcField[] = [
        { name: 'a', label: 'A', default: 'da' },
        { name: 'b', label: 'B' },
      ]
      expect(initialValues(fields)).toEqual({ a: 'da', b: '' })
    })
  })

  describe('missingRequired', () => {
    const fields: UcField[] = [
      { name: 'text', label: '本文', required: true },
      { name: 'note', label: 'メモ' },
      { name: 'title', label: 'タイトル', required: true },
    ]

    it('returns labels of required fields that are empty or whitespace', () => {
      expect(missingRequired(fields, { text: '', note: 'x', title: '   ' })).toEqual([
        '本文',
        'タイトル',
      ])
    })

    it('returns empty when all required fields are filled', () => {
      expect(missingRequired(fields, { text: 'あ', title: 'い' })).toEqual([])
    })

    it('ignores non-required empty fields', () => {
      expect(missingRequired(fields, { text: 'a', title: 'b', note: '' })).toEqual([])
    })
  })
})

describe('UcForm render', () => {
  const fields: UcField[] = [
    { name: 'text', label: '本文', type: 'textarea', required: true },
    { name: 'length', label: '長さ', type: 'select', options: ['短く', '普通'] },
    { name: 'site', label: 'URL', type: 'url' },
  ]

  it('renders a required marker only for required fields', () => {
    render(<UcForm fields={fields} values={{}} onChange={() => {}} />)
    // 必須(*)は required:true のフィールドのみ
    const stars = screen.getAllByText('*')
    expect(stars).toHaveLength(1)
    expect(screen.getByText('本文')).toBeInTheDocument()
    expect(screen.getByText('長さ')).toBeInTheDocument()
  })

  it('renders select options', () => {
    render(<UcForm fields={fields} values={{}} onChange={() => {}} />)
    expect(screen.getByRole('option', { name: '短く' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: '普通' })).toBeInTheDocument()
  })

  it('calls onChange with field name and new value on input', () => {
    const onChange = vi.fn()
    const { container } = render(
      <UcForm fields={fields} values={{ text: '' }} onChange={onChange} />,
    )
    const textarea = container.querySelector('textarea')!
    fireEvent.change(textarea, { target: { value: 'こんにちは' } })
    expect(onChange).toHaveBeenCalledWith('text', 'こんにちは')
  })
})
