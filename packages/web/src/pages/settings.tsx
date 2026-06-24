import { useState } from 'react'
import { loadBranding, type Branding } from '../branding'
import { PageContainer } from '../components/layout'
import { Toast } from '../components/gallery'
import { usePrefs, type Lang } from '../prefs'

export default function Settings({ onBranding }: { onBranding: (b: Branding) => void }) {
  const { t, lang, setLang, dark, setDark } = usePrefs()
  const [toast, setToast] = useState<string | null>(null)

  const swap = async (path: string) => {
    const b = await loadBranding(path)
    onBranding(b)
    setToast(b.productName)
  }

  const row = 'flex items-center justify-between border-b border-line py-3 text-sm'

  return (
    <PageContainer icon="settings" title={t('settings.title')}>
      <div className="rounded-rw border border-line bg-surface px-5 py-1 shadow-rw">
        <div className={row}>
          <span className="font-medium">{t('settings.theme')}</span>
          <div className="flex gap-1">
            <button
              onClick={() => setDark(false)}
              className={`rounded-rw border px-3 py-1 ${!dark ? 'border-action bg-action-soft' : 'border-line text-ink-muted'}`}
            >
              {t('settings.theme.light')}
            </button>
            <button
              onClick={() => setDark(true)}
              className={`rounded-rw border px-3 py-1 ${dark ? 'border-action bg-action-soft' : 'border-line text-ink-muted'}`}
            >
              {t('settings.theme.dark')}
            </button>
          </div>
        </div>
        <div className={row}>
          <span className="font-medium">{t('settings.lang')}</span>
          <select
            value={lang}
            onChange={(e) => setLang(e.target.value as Lang)}
            className="rounded-rw border border-line bg-surface px-2 py-1 outline-none focus:border-action"
          >
            <option value="ja">日本語</option>
            <option value="en">English</option>
          </select>
        </div>
        <div className="flex items-center justify-between py-3 text-sm">
          <span className="font-medium">{t('settings.branding')}</span>
          <div className="flex gap-1">
            <button
              onClick={() => swap('/branding.json')}
              className="rounded-rw border border-line px-3 py-1 hover:border-action"
            >
              {t('settings.branding.default')}
            </button>
            <button
              onClick={() => swap('/branding-custom-example.json')}
              className="rounded-rw border border-line px-3 py-1 hover:border-action"
            >
              {t('settings.branding.custom')}
            </button>
          </div>
        </div>
      </div>
      {toast && <Toast message={toast} onClose={() => setToast(null)} />}
    </PageContainer>
  )
}
