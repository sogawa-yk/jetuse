/** 表示設定(言語/ダークモード) + 最小i18n。localStorage永続化 */
/* eslint-disable react-refresh/only-export-components -- contextとhookの同居ファイル */
import {
  createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode,
} from 'react'
import { ja } from './i18n/dict.ja'
import { en } from './i18n/dict.en'

export type Lang = 'ja' | 'en'

const DICT = { ja, en } satisfies Record<Lang, Record<string, string>>

type Key = keyof typeof DICT.ja

type Prefs = {
  lang: Lang
  setLang: (l: Lang) => void
  dark: boolean
  setDark: (d: boolean) => void
  t: (k: Key) => string
}

const PrefsContext = createContext<Prefs | null>(null)

export function PrefsProvider({ children }: { children: ReactNode }) {
  const [lang, setLangState] = useState<Lang>(
    () => (localStorage.getItem('jetuse.lang') as Lang) ?? 'ja',
  )
  const [dark, setDarkState] = useState(() => localStorage.getItem('jetuse.dark') === '1')

  useEffect(() => {
    document.documentElement.classList.toggle('dark', dark)
    // tokens.css(Redwood抽出)のダーク変数は [data-theme="dark"] で切り替わる(UI-02)
    document.documentElement.dataset.theme = dark ? 'dark' : 'light'
  }, [dark])

  const setLang = useCallback((l: Lang) => {
    localStorage.setItem('jetuse.lang', l)
    setLangState(l)
  }, [])
  const setDark = useCallback((d: boolean) => {
    localStorage.setItem('jetuse.dark', d ? '1' : '0')
    setDarkState(d)
  }, [])
  const t = useCallback((k: Key) => DICT[lang][k] ?? k, [lang])

  const value = useMemo(
    () => ({ lang, setLang, dark, setDark, t }),
    [lang, setLang, dark, setDark, t],
  )
  return <PrefsContext.Provider value={value}>{children}</PrefsContext.Provider>
}

export function usePrefs(): Prefs {
  const ctx = useContext(PrefsContext)
  if (!ctx) throw new Error('usePrefs must be used within PrefsProvider')
  return ctx
}
