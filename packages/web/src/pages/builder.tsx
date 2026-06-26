/** ユースケースビルダー(UC-03)。定義編集 + ライブプレビュー + 保存/削除 */
import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { authHeaders, useUser } from '../auth'
import { IconPicker, NavIcon, isIconName } from '../components/icons'
import { PageContainer } from '../components/layout'
import {
  initialValues,
  UcForm,
  type UcDefinition,
  type UcField,
} from '../components/ucform'
import { usePrefs } from '../prefs'

const FIELD_TYPES = ['text', 'textarea', 'select', 'number', 'url'] as const

const emptyField = (n: number): UcField => ({
  name: `field${n}`,
  label: `項目${n}`,
  type: 'text',
  required: false,
})

const inputCls =
  'rounded-rw border border-line bg-surface px-2 py-1.5 text-sm outline-none focus:border-action'

export default function Builder() {
  const { id } = useParams()
  const { t } = usePrefs()
  const user = useUser()
  const nav = useNavigate()
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [icon, setIcon] = useState('document')
  const [tags, setTags] = useState('')
  const [visibility, setVisibility] = useState<'private' | 'public'>('private')
  const [model, setModel] = useState('')
  const [models, setModels] = useState<{ key: string; label: string }[]>([])
  const [fields, setFields] = useState<UcField[]>([emptyField(1)])
  const [template, setTemplate] = useState('')
  const [previewValues, setPreviewValues] = useState<Record<string, string>>({})
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [publishMsg, setPublishMsg] = useState<string | null>(null)
  const [publishing, setPublishing] = useState(false)

  useEffect(() => {
    fetch('/api/chat/models', { headers: authHeaders(user) })
      .then((r) => r.json())
      .then((d) => setModels(d.models))
      .catch(() => setModels([]))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (!id) return
    fetch(`/api/usecases/${id}`, { headers: authHeaders(user) })
      .then((r) => r.json())
      .then((d: UcDefinition) => {
        setName(d.name)
        setDescription(d.description ?? '')
        setIcon(d.icon ?? '🧩')
        setTags((d.tags ?? []).join(','))
        setVisibility(d.visibility ?? 'private')
        setModel(d.model ?? '')
        setFields(d.fields)
        setTemplate(d.template)
      })
      .catch(() => setError(t('uc.notFound')))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id])

  // プレビュー値: フィールド定義の既定値の上にユーザー入力を重ねる(リセット用effect不要)
  const mergedPreview = { ...initialValues(fields), ...previewValues }

  const patchField = (i: number, patch: Partial<UcField>) =>
    setFields((cur) => cur.map((f, j) => (j === i ? { ...f, ...patch } : f)))

  const moveField = (i: number, d: -1 | 1) =>
    setFields((cur) => {
      const next = [...cur]
      const j = i + d
      if (j < 0 || j >= next.length) return cur
      ;[next[i], next[j]] = [next[j], next[i]]
      return next
    })

  const save = async () => {
    setSaving(true)
    setError(null)
    const body = {
      name: name.trim(),
      description: description.trim() || null,
      icon: icon.trim() || null,
      tags: tags.split(',').map((s) => s.trim()).filter(Boolean),
      visibility,
      model: model || null,
      // 空の選択肢は保存時に除去
      fields: fields.map((f) =>
        f.type === 'select'
          ? { ...f, options: (f.options ?? []).map((s) => s.trim()).filter(Boolean) }
          : f,
      ),
      template,
    }
    try {
      const res = await fetch(id ? `/api/usecases/${id}` : '/api/usecases', {
        method: id ? 'PUT' : 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders(user) },
        body: JSON.stringify(body),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail))
      nav(`/uc/${data.id ?? id}`)
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e))
    } finally {
      setSaving(false)
    }
  }

  const remove = async () => {
    if (!id || !window.confirm(t('uc.deleteConfirm'))) return
    await fetch(`/api/usecases/${id}`, { method: 'DELETE', headers: authHeaders(user) })
    nav('/')
  }

  // PLG-05: マーケットに公開(export→署名→publish はサーバー側 /publish が担う)
  const publish = async () => {
    if (!id) return
    const version = window.prompt(t('market.publish.prompt'), '1.0.0')
    if (!version) return
    setPublishing(true)
    setPublishMsg(null)
    try {
      const res = await fetch(`/api/usecases/${id}/publish`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders(user) },
        body: JSON.stringify({ version: version.trim() }),
      })
      const data = await res.json()
      if (!res.ok)
        throw new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail))
      setPublishMsg(`${t('market.publish.ok')}: ${data.id}@${data.version}`)
    } catch (e) {
      setPublishMsg(`${t('market.publish.fail')}: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setPublishing(false)
    }
  }

  return (
    <PageContainer
      wide
      icon="🛠"
      title={id ? t('uc.builder.edit') : t('uc.builder.new')}
      subtitle={t('uc.builder.lead')}
      action={
        id ? (
          <div className="flex items-center gap-2">
            <button
              onClick={() => void publish()}
              disabled={publishing}
              className="rounded-rw border border-action px-3 py-1.5 text-sm text-action hover:bg-action hover:text-white disabled:opacity-40"
            >
              {t('market.publish')}
            </button>
            <button
              onClick={() => void remove()}
              className="rounded-rw border border-primary px-3 py-1.5 text-sm text-primary hover:bg-primary-strong hover:text-white"
            >
              {t('chat.preset.delete')}
            </button>
          </div>
        ) : undefined
      }
    >
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <div className="space-y-4">
          {/* 基本情報 */}
          <section className="rounded-rw border border-line bg-surface p-4">
            <h2 className="mb-3 text-sm font-semibold text-ink-muted">{t('uc.builder.basic')}</h2>
            <div className="mb-3">
              <span className="mb-1.5 block text-xs font-medium text-ink-muted">
                {t('uc.builder.icon')}
              </span>
              <IconPicker value={icon} onChange={setIcon} />
            </div>
            <div className="flex gap-2">
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder={t('uc.builder.name')}
                className={`${inputCls} flex-1`}
              />
            </div>
            <input
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder={t('uc.builder.desc')}
              className={`${inputCls} mt-2 w-full`}
            />
            <div className="mt-2 flex flex-wrap gap-2">
              <input
                value={tags}
                onChange={(e) => setTags(e.target.value)}
                placeholder={t('uc.builder.tags')}
                className={`${inputCls} min-w-32 flex-1`}
              />
              <select
                value={model}
                onChange={(e) => setModel(e.target.value)}
                className={inputCls}
                aria-label="model"
                title={t('uc.builder.model')}
              >
                <option value="">{t('uc.builder.model.default')}</option>
                {models.map((m) => (
                  <option key={m.key} value={m.key}>
                    {m.label}
                  </option>
                ))}
              </select>
              <select
                value={visibility}
                onChange={(e) => setVisibility(e.target.value as 'private' | 'public')}
                className={inputCls}
                aria-label="visibility"
              >
                <option value="private">{t('uc.builder.private')}</option>
                <option value="public">{t('uc.builder.public')}</option>
              </select>
            </div>
          </section>

          {/* 入力フィールド定義 */}
          <section className="rounded-rw border border-line bg-surface p-4">
            <h2 className="mb-3 text-sm font-semibold text-ink-muted">{t('uc.builder.fields')}</h2>
            <div className="space-y-3">
              {fields.map((f, i) => (
                <div key={i} className="rounded-rw border border-line bg-bg p-2.5">
                  <div className="flex flex-wrap items-center gap-2">
                    <input
                      value={f.name}
                      onChange={(e) => patchField(i, { name: e.target.value })}
                      placeholder="name"
                      className={`${inputCls} w-28 font-mono text-xs`}
                    />
                    <input
                      value={f.label}
                      onChange={(e) => patchField(i, { label: e.target.value })}
                      placeholder={t('uc.builder.fieldLabel')}
                      className={`${inputCls} flex-1`}
                    />
                    <select
                      value={f.type ?? 'text'}
                      onChange={(e) => patchField(i, { type: e.target.value as UcField['type'] })}
                      className={inputCls}
                      aria-label="type"
                    >
                      {FIELD_TYPES.map((ty) => (
                        <option key={ty} value={ty}>
                          {ty}
                        </option>
                      ))}
                    </select>
                    <label className="flex items-center gap-1 text-xs">
                      <input
                        type="checkbox"
                        checked={f.required ?? false}
                        onChange={(e) => patchField(i, { required: e.target.checked })}
                      />
                      {t('uc.builder.required')}
                    </label>
                  </div>
                  {f.type === 'select' && (
                    <div className="mt-2 space-y-1.5">
                      <span className="block text-xs text-ink-muted">
                        {t('uc.builder.options')}
                      </span>
                      {(f.options ?? []).map((o, oi) => (
                        <div key={oi} className="flex items-center gap-1.5">
                          <input
                            value={o}
                            onChange={(e) =>
                              patchField(i, {
                                options: (f.options ?? []).map((x, xi) =>
                                  xi === oi ? e.target.value : x,
                                ),
                              })
                            }
                            placeholder={`${t('uc.builder.option')} ${oi + 1}`}
                            className={`${inputCls} flex-1`}
                          />
                          <button
                            onClick={() =>
                              patchField(i, {
                                options: (f.options ?? []).filter((_, xi) => xi !== oi),
                              })
                            }
                            className="px-1 text-xs text-ink-muted hover:text-primary-strong"
                            aria-label="remove option"
                          >
                            ✕
                          </button>
                        </div>
                      ))}
                      <button
                        onClick={() =>
                          patchField(i, { options: [...(f.options ?? []), ''] })
                        }
                        className="rounded-rw border border-line px-2 py-1 text-xs hover:border-action hover:text-action"
                      >
                        ＋ {t('uc.builder.addOption')}
                      </button>
                    </div>
                  )}
                  <div className="mt-2 flex items-center gap-2 text-xs">
                    <button
                      onClick={() => setTemplate((cur) => `${cur}{{${f.name}}}`)}
                      className="rounded-rw border border-line px-2 py-0.5 hover:border-action hover:text-action"
                    >
                      {t('uc.builder.insertVar')}
                    </button>
                    <span className="font-mono text-ink-muted">{`{{${f.name}}}`}</span>
                    <span className="ml-auto flex gap-1">
                      <button onClick={() => moveField(i, -1)} className="px-1 hover:text-action">↑</button>
                      <button onClick={() => moveField(i, 1)} className="px-1 hover:text-action">↓</button>
                      <button
                        onClick={() => setFields((cur) => cur.filter((_, j) => j !== i))}
                        className="px-1 text-ink-muted hover:text-primary-strong"
                      >
                        ✕
                      </button>
                    </span>
                  </div>
                </div>
              ))}
            </div>
            <button
              onClick={() => setFields((cur) => [...cur, emptyField(cur.length + 1)])}
              className="mt-3 rounded-rw border border-line px-3 py-1.5 text-sm hover:border-action hover:text-action"
            >
              ＋ {t('uc.builder.addField')}
            </button>
          </section>

          {/* プロンプトテンプレート */}
          <section className="rounded-rw border border-line bg-surface p-4">
            <h2 className="mb-2 text-sm font-semibold text-ink-muted">
              {t('uc.builder.template')}
            </h2>
            <textarea
              rows={7}
              value={template}
              onChange={(e) => setTemplate(e.target.value)}
              placeholder={t('uc.builder.templatePh')}
              className={`${inputCls} w-full resize-y font-mono text-xs leading-relaxed`}
            />
          </section>

          <div className="flex items-center gap-3">
            <button
              onClick={() => void save()}
              disabled={saving || !name.trim() || !template.trim() || fields.length === 0}
              className="rounded-rw bg-cta px-4 py-2 text-sm font-medium text-cta-ink hover:bg-cta-strong disabled:opacity-40"
            >
              {t('uc.builder.save')}
            </button>
            {error && <span className="text-xs text-primary-strong">⚠ {error}</span>}
            {publishMsg && <span className="text-xs text-ink-muted">{publishMsg}</span>}
          </div>
        </div>

        {/* ライブプレビュー(実行ページと同一レンダラ) */}
        <div className="rounded-rw border border-dashed border-line bg-surface p-4 lg:sticky lg:top-4 lg:self-start">
          <h2 className="mb-3 text-sm font-semibold text-ink-muted">{t('uc.builder.preview')}</h2>
          <div className="mb-3 flex items-center gap-2">
            {isIconName(icon) ? (
              <span className="flex h-9 w-9 items-center justify-center rounded-rw bg-band/10 text-band">
                <NavIcon name={icon} className="h-5 w-5" />
              </span>
            ) : (
              <span className="text-2xl">{icon || '🧩'}</span>
            )}
            <div>
              <div className="font-medium">{name || t('uc.builder.name')}</div>
              <div className="text-xs text-ink-muted">{description}</div>
            </div>
          </div>
          <UcForm
            fields={fields}
            values={mergedPreview}
            onChange={(n, v) => setPreviewValues((cur) => ({ ...cur, [n]: v }))}
          />
        </div>
      </div>
    </PageContainer>
  )
}
