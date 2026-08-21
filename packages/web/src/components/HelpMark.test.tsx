import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { HelpMark } from './HelpMark'
import { HELP_CONTENT, type HelpKey } from './helpContent'
import { PrefsProvider } from '../prefs'
import { ja } from '../i18n/dict.ja'
import { en } from '../i18n/dict.en'

/** ヘルプマーク+構成図ポップアップ(feedback 20260620 #4)。 */
describe('helpContent registry', () => {
  it('every entry has a usecase diagram path and i18n keys present in ja/en', () => {
    for (const entry of Object.values(HELP_CONTENT)) {
      expect(entry.diagram).toMatch(/^\/architecture\/usecase-[a-z]+\.png$/)
      expect(ja[entry.titleKey as keyof typeof ja]).toBeTruthy()
      expect(ja[entry.descKey as keyof typeof ja]).toBeTruthy()
      expect(en[entry.titleKey as keyof typeof en]).toBeTruthy()
      expect(en[entry.descKey as keyof typeof en]).toBeTruthy()
    }
  })
})

describe('HelpMark', () => {
  const renderMark = (topic: HelpKey) =>
    render(
      <PrefsProvider>
        <HelpMark topic={topic} />
      </PrefsProvider>,
    )

  it('opens a dialog with the diagram and closes on ✕', () => {
    renderMark('dbchat')
    // 初期はダイアログなし
    expect(screen.queryByRole('dialog')).toBeNull()
    // ? を押すと開く
    fireEvent.click(screen.getByRole('button', { name: ja['help.open'] }))
    const dialog = screen.getByRole('dialog')
    expect(dialog).toBeInTheDocument()
    // 構成図の画像が表示される
    const img = screen.getByRole('img', { name: ja['help.dbchat.title'] }) as HTMLImageElement
    expect(img.getAttribute('src')).toBe(HELP_CONTENT.dbchat.diagram)
    // ✕ で閉じる
    fireEvent.click(screen.getByRole('button', { name: ja['help.close'] }))
    expect(screen.queryByRole('dialog')).toBeNull()
  })
})
