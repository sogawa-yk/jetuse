/** 条件パネル(要求5「一覧から条件を選択して探せる」)。
 *
 *  集合が決まっている項目(屋内外・昼夜・分析状態・人物・確認)は**選択式**にする ——
 *  綴りを打たせると 0 件なのか綴り違いなのかが利用者に判らない(API は 422 を返すが、
 *  そもそも打たせない)。自由入力は所属・カテゴリ・場所・権利・タグ・期間・尺。
 */
import type { ReactNode } from 'react'
import { usePrefs } from '../../prefs'
import type { SearchForm, TriState } from './format'

type Setter = <K extends keyof SearchForm>(key: K, value: SearchForm[K]) => void

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="flex min-w-0 flex-col gap-1">
      <span className="text-[11px] text-ink-muted">{label}</span>
      {children}
    </label>
  )
}

const INPUT =
  'w-full rounded-rw border border-line bg-bg px-2 py-1.5 text-sm outline-none focus:border-action'

export function FilterPanel({
  form, onChange, invalidFields = [],
}: { form: SearchForm; onChange: Setter; invalidFields?: (keyof SearchForm)[] }) {
  const { t } = usePrefs()
  const text = (key: keyof SearchForm, label: string, placeholder = '') => (
    <Field label={label}>
      <input
        value={String(form[key])}
        onChange={(e) => onChange(key, e.target.value as never)}
        placeholder={placeholder}
        aria-label={label}
        aria-invalid={invalidFields.includes(key) || undefined}
        className={`${INPUT} ${invalidFields.includes(key) ? 'border-primary-strong' : ''}`}
      />
    </Field>
  )
  const tri = (key: 'has_people' | 'confirmed', label: string, yes: string, no: string) => (
    <Field label={label}>
      <select
        value={form[key]}
        onChange={(e) => onChange(key, e.target.value as TriState)}
        aria-label={label}
        className={INPUT}
      >
        <option value="">{t('videos.opt.any')}</option>
        <option value="true">{yes}</option>
        <option value="false">{no}</option>
      </select>
    </Field>
  )

  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-3">
      {text('collection', t('videos.f.collection'))}
      {text('category', t('videos.f.category'))}
      {text('place', t('videos.f.place'))}
      <Field label={t('videos.f.indoor')}>
        <select
          value={form.indoor}
          onChange={(e) => onChange('indoor', e.target.value)}
          aria-label={t('videos.f.indoor')}
          className={INPUT}
        >
          <option value="">{t('videos.opt.any')}</option>
          <option value="indoor">{t('videos.opt.indoor')}</option>
          <option value="outdoor">{t('videos.opt.outdoor')}</option>
          {/* `unknown` は NULL(未分析)とは別の値 —— 「分析したが判らなかった」場面を
              名指しで探せるようにする(specs/20 §1) */}
          <option value="unknown">{t('videos.opt.unknown')}</option>
        </select>
      </Field>
      <Field label={t('videos.f.timeOfDay')}>
        <select
          value={form.time_of_day}
          onChange={(e) => onChange('time_of_day', e.target.value)}
          aria-label={t('videos.f.timeOfDay')}
          className={INPUT}
        >
          <option value="">{t('videos.opt.any')}</option>
          <option value="day">{t('videos.opt.day')}</option>
          <option value="night">{t('videos.opt.night')}</option>
          <option value="unknown">{t('videos.opt.unknown')}</option>
        </select>
      </Field>
      {tri('has_people', t('videos.f.hasPeople'), t('videos.opt.yes'), t('videos.opt.no'))}
      {text('tags', t('videos.f.tags'), '雨、屋外')}
      {text('rights', t('videos.f.rights'))}
      <Field label={t('videos.f.analysisState')}>
        <select
          value={form.analysis_state}
          onChange={(e) => onChange('analysis_state', e.target.value)}
          aria-label={t('videos.f.analysisState')}
          className={INPUT}
        >
          <option value="">{t('videos.opt.any')}</option>
          {(['pending', 'running', 'done', 'failed', 'partial'] as const).map((s) => (
            <option key={s} value={s}>
              {t(`videos.state.${s}` as Parameters<typeof t>[0])}
            </option>
          ))}
        </select>
      </Field>
      {text('captured_from', t('videos.f.capturedFrom'), '2026-01-01')}
      {text('captured_to', t('videos.f.capturedTo'), '2026-12-31')}
      {text('created_from', t('videos.f.createdFrom'), '2026-01-01')}
      {text('created_to', t('videos.f.createdTo'), '2026-12-31')}
      {text('duration_min_sec', t('videos.f.durationMin'), '0')}
      {text('duration_max_sec', t('videos.f.durationMax'), '600')}
      {tri(
        'confirmed', t('videos.f.confirmed'),
        t('videos.opt.confirmedYes'), t('videos.opt.confirmedNo'),
      )}
    </div>
  )
}
