/** 場面の横断検索(VID-06 / specs/20 §6 の `/videos/search`)。
 *
 *  返ってくるのは**場面**であって映像ではない。カードは必ず
 *  「一致理由」(要求11)と「その時刻から再生」(要求6)を持つ ——
 *  理由の無い結果と、移動できない結果は出さない(tasks/VID-06 禁止事項)。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useUser } from '../../auth'
import { PageContainer } from '../../components/layout'
import { OciButton, Panel } from '../../components/oci'
import { usePrefs } from '../../prefs'
import { errorText, searchScenes } from './api'
import { FilterPanel } from './FilterPanel'
import { SceneCard } from './SceneCard'
import {
  buildSearchBody, EMPTY_SEARCH_FORM, hasAnyCondition, SearchFormError,
  type SearchForm,
} from './format'
import type { SearchResult } from './types'

const LIMITS = [10, 20, 50, 100]

export default function VideoSearch() {
  const { t } = usePrefs()
  const user = useUser()
  const [params, setParams] = useSearchParams()
  const [form, setForm] = useState<SearchForm>({
    ...EMPTY_SEARCH_FORM, q: params.get('q') ?? '',
  })
  const [openConditions, setOpenConditions] = useState(false)
  const [result, setResult] = useState<SearchResult | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [invalid, setInvalid] = useState<(keyof SearchForm)[]>([])
  // 類似検索(要求10)。**何で引いているかを画面に出す**(黙って別の検索にしない)
  const similarOf = params.get('similar')

  const set: <K extends keyof SearchForm>(key: K, value: SearchForm[K]) => void = (
    key, value,
  ) => setForm((cur) => ({ ...cur, [key]: value }))

  const run = useCallback(
    async (current: SearchForm, similar: string | null) => {
      setBusy(true)
      setError(null)
      setInvalid([])
      try {
        const body = buildSearchBody(current, { similarToSceneId: similar })
        setResult(await searchScenes(user, body))
      } catch (e) {
        if (e instanceof SearchFormError) {
          setInvalid([e.field])
          setError(e.field === 'tags' ? t('videos.tagsError') : t('videos.numberError'))
        } else {
          setError(errorText(e))
        }
        setResult(null)
      } finally {
        setBusy(false)
      }
    },
    [user, t],
  )

  // **何で引いているかは URL が持つ**(`?q=` / `?similar=`)。結果カードから詳細へ進んで
  // 戻ってきたときに、同じ結果へ戻れるようにするため —— 戻るたびに打ち直しでは、
  // 「気になる場面を順に開く」という使い方ができない。
  // 条件パネルの絞り込みまでは URL に載せていない(載せるなら条件側も URL を正本に
  // する必要があり、v1 の範囲を超える)。押した検索は `lastRan` で二重実行を防ぐ。
  const qParam = params.get('q') ?? ''
  // 鍵は**連結しない**。検索語に区切り文字(`|`)が入ると分解で切り落とされ、
  // 画面に出ている結果と検索語が食い違う(`設備|点検` が `設備` の結果になる)
  const urlKey = JSON.stringify([similarOf ?? '', qParam])
  // **いま表示している結果がどの URL のものか。** 押した検索(条件パネル込み)を
  // URL 由来の検索が上書きしないための印。「一度走らせた」フラグにはしない ——
  // React の StrictMode は effect を 2 回呼ぶので、1 回目を印で止めると
  // 2 回目が捨てられて**結果が出ないまま**になる(実ブラウザで踏んだ)。
  const shownFor = useRef<string | null>(null)

  // effect の中では同期に setState しない(promise の連鎖で受ける)
  useEffect(() => {
    if (shownFor.current === urlKey) return  // 押した検索の結果が出ている
    if (!similarOf && !qParam) return
    let live = true
    searchScenes(user, buildSearchBody(
      { ...EMPTY_SEARCH_FORM, q: qParam },
      { similarToSceneId: similarOf },
    ))
      .then((r) => {
        if (!live) return
        shownFor.current = urlKey
        setResult(r)
        setError(null)
      })
      .catch((e) => { if (live) { setResult(null); setError(errorText(e)) } })
    return () => { live = false }
  }, [urlKey, similarOf, qParam, user])

  const submit = (e: React.FormEvent) => {
    e.preventDefault()
    const q = form.q.trim()
    // 押した検索は条件パネルも含めてここで実行する。URL には検索語だけを載せ、
    // その鍵の結果は「表示済み」にして URL 由来の検索と二重にしない
    shownFor.current = JSON.stringify(['', q])
    setParams(q ? { q } : {})
    void run(form, null)
  }

  const onSimilar = (sceneId: string) => setParams({ similar: sceneId })

  return (
    <PageContainer
      wide
      icon="search"
      title={t('videos.toSearch')}
      subtitle={t('videos.search.lead')}
      action={
        <Link
          to="/videos"
          className="rounded-rw border border-line px-3.5 py-1.5 text-sm hover:border-action hover:text-action"
        >
          {t('videos.toList')}
        </Link>
      }
    >
      <div className="space-y-4">
        <form onSubmit={submit}>
          <Panel>
            <div className="space-y-3">
              <label className="flex flex-col gap-1">
                <span className="text-[11px] text-ink-muted">{t('videos.search.q')}</span>
                <div className="flex flex-wrap items-center gap-2">
                  <input
                    value={form.q}
                    onChange={(e) => set('q', e.target.value)}
                    placeholder={t('videos.search.placeholder')}
                    aria-label={t('videos.search.q')}
                    data-testid="search-q"
                    className="min-w-0 flex-1 rounded-rw border border-line bg-bg px-3 py-2 text-sm outline-none focus:border-action"
                  />
                  <label className="flex items-center gap-1 text-xs text-ink-muted">
                    {t('videos.search.limit')}
                    <select
                      value={form.limit}
                      onChange={(e) => set('limit', Number(e.target.value))}
                      aria-label={t('videos.search.limit')}
                      className="rounded-rw border border-line bg-bg px-2 py-1.5 text-xs"
                    >
                      {LIMITS.map((n) => <option key={n} value={n}>{n}</option>)}
                    </select>
                  </label>
                  <OciButton type="submit" disabled={busy}>
                    {busy ? t('videos.search.running') : t('videos.search.run')}
                  </OciButton>
                </div>
              </label>

              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => setOpenConditions((v) => !v)}
                  aria-expanded={openConditions}
                  className="rounded-rw border border-line px-3 py-1.5 text-xs text-ink-muted hover:border-action hover:text-action"
                >
                  {openConditions ? '▾' : '▸'} {t('videos.search.conditions')}
                </button>
                {hasAnyCondition({ ...form, q: '' }) && (
                  <button
                    type="button"
                    onClick={() => setForm({ ...EMPTY_SEARCH_FORM, q: form.q, limit: form.limit })}
                    className="text-xs text-action hover:underline"
                  >
                    {t('videos.search.reset')}
                  </button>
                )}
              </div>
              {openConditions && (
                <FilterPanel form={form} onChange={set} invalidFields={invalid} />
              )}
            </div>
          </Panel>
        </form>

        {similarOf && (
          <div className="flex items-center gap-3 rounded-rw border border-line bg-surface px-3 py-2 text-xs">
            <span>{t('videos.search.similarOf')}</span>
            <button
              type="button"
              onClick={() => { setParams({}); setResult(null) }}
              className="text-action hover:underline"
            >
              {t('videos.search.clearSimilar')}
            </button>
          </div>
        )}

        {error && (
          <p className="rounded-rw bg-pill-err px-3 py-2 text-sm text-pill-err-ink">⚠ {error}</p>
        )}

        {result && (
          <div className="space-y-3">
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-muted">
              <span className="rounded-full border border-line bg-surface px-2 py-0.5">
                {t(`videos.search.mode.${result.mode}` as Parameters<typeof t>[0])}
              </span>
              {/* **全一致件数**を出す(返した行数ではない。API の `total`) */}
              <span className="tabular-nums" data-testid="search-total">
                {result.total} {t('videos.search.of')} {result.returned}{' '}
                {t('videos.search.shownSuffix')}
              </span>
              {result.excluded_no_vector > 0 && (
                <span className="text-primary-strong">
                  {result.excluded_no_vector} {t('videos.search.noVector')}
                </span>
              )}
            </div>

            {result.hits.length === 0 ? (
              <p className="py-10 text-center text-sm text-ink-muted/70">
                {t('videos.search.empty')}
              </p>
            ) : (
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
                {result.hits.map((hit) => (
                  <SceneCard key={hit.scene_id} hit={hit} onSimilar={onSimilar} />
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </PageContainer>
  )
}
