/** 入力欄まわり(chat.tsx分割: review-validation.md §5)。
 *  エージェント/設定/ツール/モデル/添付のコントロール段、生成パラメータ設定パネル、
 *  ツール・MCP選択パネル、画像プレビュー、入力テキストエリア＋送信/停止 を担う。
 *  状態はすべてコンテナ(chat.tsx)が保持し、Composerは描画とイベント転送に徹する。 */
import { type RefObject } from 'react'
import { Link } from 'react-router-dom'
import { NavIcon } from '../../components/icons'
import { usePrefs } from '../../prefs'
import type { ModelInfo, Preset } from './types'

// 匿名利用可能な公開MCPのプリセット(実機確認済み — AGT-01c)
const MCP_PRESETS = [
  { label: 'deepwiki', url: 'https://mcp.deepwiki.com/mcp', desc: 'GitHubリポジトリ解説' },
  { label: 'Microsoft Learn', url: 'https://learn.microsoft.com/api/mcp', desc: 'MS公式ドキュメント' },
]

export type ComposerProps = {
  // エージェント
  agentDef: { id: string; name: string; icon?: string | null } | null
  agentList: { id: string; name: string; icon?: string | null }[]
  agentId: string | null
  switchAgent: (id: string) => void
  // 通知
  notice: string | null
  setNotice: (v: string | null) => void
  // ツール/MCP パネル
  showTools: boolean
  setShowTools: (v: boolean) => void
  autoTools: boolean
  setAutoTools: (v: boolean) => void
  availableTools: { name: string; label: string; description: string; builtin: boolean }[]
  selectedTools: string[]
  setSelectedTools: (fn: (cur: string[]) => string[]) => void
  mcpServers: { id: string; label: string; url: string }[]
  selectedMcp: string[]
  setSelectedMcp: (fn: (cur: string[]) => string[]) => void
  mcpLabel: string
  setMcpLabel: (v: string) => void
  mcpUrl: string
  setMcpUrl: (v: string) => void
  mcpError: string | null
  addMcpServer: () => void
  removeMcpServer: (id: string) => void
  // 設定パネル
  showSettings: boolean
  setShowSettings: (v: boolean) => void
  temperature: number
  setTemperature: (v: number) => void
  topP: number
  setTopP: (v: number) => void
  maxTokens: string
  setMaxTokens: (v: string) => void
  minMaxTokens: number
  isReasoning: boolean
  effort: string
  setEffort: (v: string) => void
  systemPrompt: string
  setSystemPrompt: (v: string) => void
  presets: Preset[]
  presetName: string
  setPresetName: (v: string) => void
  selectedPreset: string
  setSelectedPreset: (v: string) => void
  savePreset: () => void
  deletePreset: () => void
  // 画像
  images: string[]
  setImages: (fn: (cur: string[]) => string[]) => void
  isVision: boolean
  addImages: (files: FileList | null) => void
  // モデル
  model: string
  setModel: (v: string) => void
  models: ModelInfo[]
  // 入力・送信
  input: string
  setInput: (v: string) => void
  taRef: RefObject<HTMLTextAreaElement | null>
  busy: boolean
  send: () => void
  stop: () => void
}

export function Composer(p: ComposerProps) {
  const { t } = usePrefs()
  // ref/入力欄まわりは props オブジェクト経由だと react-hooks/refs が誤検知するため
  // ローカルへ取り出す(taRefは本物のref、それ以外は素のstring/関数)。
  const { taRef, input, busy } = p
  return (
    <div className="border-t border-line bg-surface px-4 py-3">
      {p.agentDef && (
        <div className="mx-auto mb-2 flex max-w-3xl items-center gap-2 rounded-rw border border-action bg-action-soft px-3 py-1.5 text-xs">
          <span>
            {p.agentDef.icon || '🤖'} <b>{p.agentDef.name}</b>（{t('agent.badge')}）
          </span>
          <Link to="/chat" className="ml-auto underline hover:text-action">
            {t('agent.exit')}
          </Link>
        </div>
      )}
      {p.notice && (
        <div className="mx-auto mb-2 flex max-w-3xl items-center justify-between rounded-rw border border-primary bg-primary-soft px-3 py-1.5 text-xs">
          <span>⚠ {p.notice}</span>
          <button onClick={() => p.setNotice(null)} aria-label="dismiss" className="px-1">
            ✕
          </button>
        </div>
      )}
      {p.showTools && <ToolsPanel {...p} />}
      {p.showSettings && <SettingsPanel {...p} />}
      {p.images.length > 0 && (
        <div className="mx-auto mb-2 flex max-w-3xl flex-wrap gap-2">
          {p.images.map((u, i) => (
            <span key={i} className="relative">
              <img src={u} alt="" className="h-14 rounded-rw border border-line object-cover" />
              <button
                type="button"
                aria-label="remove image"
                onClick={() => p.setImages((cur) => cur.filter((_, j) => j !== i))}
                className="absolute -right-1.5 -top-1.5 flex h-4 w-4 items-center justify-center rounded-full bg-cta text-[9px] text-cta-ink"
              >
                ✕
              </button>
            </span>
          ))}
        </div>
      )}
      <form
        className="mx-auto flex max-w-3xl flex-col gap-2"
        onSubmit={(e) => {
          e.preventDefault()
          p.send()
        }}
      >
        {/* 上段: 各種コントロール(エージェント/設定/ツール/モデル/添付) */}
        <div className="flex flex-wrap items-center gap-2">
          {(p.agentList.length > 0 || p.agentDef) && (
            <select
              value={p.agentId ?? ''}
              onChange={(e) => p.switchAgent(e.target.value)}
              disabled={p.busy}
              className="h-9 rounded-rw border border-line bg-surface px-2 text-xs outline-none focus:border-action disabled:opacity-40"
              aria-label={t('chat.agentSelect')}
              title={t('chat.agentSelect')}
            >
              <option value="">{t('chat.agentNone')}</option>
              {p.agentList.map((a) => (
                <option key={a.id} value={a.id}>
                  {(a.icon || '🤖') + ' ' + a.name}
                </option>
              ))}
            </select>
          )}
          {!p.agentDef && (
            <button
              type="button"
              onClick={() => p.setShowSettings(!p.showSettings)}
              title={t('chat.settings')}
              aria-label={t('chat.settings')}
              className={`flex h-9 w-9 items-center justify-center rounded-rw border ${
                p.showSettings || p.systemPrompt.trim()
                  ? 'border-action bg-action-soft text-ink'
                  : 'border-line text-ink-muted hover:border-action hover:text-action'
              }`}
            >
              <NavIcon name="settings" className="h-[18px] w-[18px]" />
            </button>
          )}
          {!p.agentDef && p.isReasoning && (
            <button
              type="button"
              onClick={() => p.setShowTools(!p.showTools)}
              title={t('chat.tools')}
              aria-label={t('chat.tools')}
              className={`flex h-9 items-center justify-center gap-1 rounded-rw border px-2.5 ${
                p.showTools || p.selectedTools.length > 0
                  ? 'border-action bg-action-soft text-ink'
                  : 'border-line text-ink-muted hover:border-action hover:text-action'
              }`}
            >
              <NavIcon name="tools" className="h-[18px] w-[18px]" />
              {p.selectedTools.length + p.selectedMcp.length > 0 && (
                <span className="text-xs">{p.selectedTools.length + p.selectedMcp.length}</span>
              )}
            </button>
          )}
          {!p.agentDef && (
            <select
              value={p.model}
              onChange={(e) => p.setModel(e.target.value)}
              className="h-9 rounded-rw border border-line bg-surface px-2 text-xs outline-none focus:border-action"
              aria-label="model"
            >
              {(p.models.length ? p.models : [{ key: p.model, label: p.model }]).map((m) => (
                <option key={m.key} value={m.key}>
                  {m.label}
                </option>
              ))}
            </select>
          )}
          {p.isVision && (
            <>
              <input
                type="file"
                accept="image/*"
                multiple
                id="chat-image-input"
                className="hidden"
                onChange={(e) => {
                  p.addImages(e.target.files)
                  e.target.value = ''
                }}
              />
              <label
                htmlFor="chat-image-input"
                title={t('chat.attachImage')}
                aria-label={t('chat.attachImage')}
                className="flex h-9 w-9 cursor-pointer items-center justify-center rounded-rw border border-line text-ink-muted hover:border-action hover:text-action"
              >
                <NavIcon name="attach" className="h-[18px] w-[18px]" />
              </label>
            </>
          )}
        </div>
        {/* 下段: 入力欄(全幅・複数行・自動拡張)＋送信 */}
        <div className="flex items-end gap-2">
          <textarea
            ref={taRef}
            rows={3}
            value={input}
            onChange={(e) => p.setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault()
                p.send()
              }
            }}
            placeholder={t('chat.placeholder')}
            className="max-h-48 min-h-[4.5rem] min-w-0 flex-1 resize-none overflow-y-auto rounded-rw border border-line bg-surface px-3 py-2 text-sm leading-relaxed outline-none focus:border-action"
          />
          {busy ? (
            <button
              type="button"
              onClick={p.stop}
              className="shrink-0 rounded-rw border border-line px-4 py-2.5 text-sm font-medium text-ink-muted hover:border-action hover:text-action"
            >
              ■ {t('chat.stop')}
            </button>
          ) : (
            <button
              type="submit"
              disabled={!input.trim()}
              className="shrink-0 rounded-rw bg-cta px-5 py-2.5 text-sm font-medium text-cta-ink transition-colors hover:bg-cta-strong disabled:cursor-not-allowed disabled:opacity-40"
            >
              {t('chat.send')}
            </button>
          )}
        </div>
      </form>
    </div>
  )
}

/** ツール・MCPサーバー選択パネル(🛠 / エージェントモード AGT-01/01b/02)。 */
function ToolsPanel(p: ComposerProps) {
  const { t } = usePrefs()
  return (
    <div className="mx-auto mb-3 max-w-3xl space-y-2 rounded-rw border border-line bg-bg p-4 text-sm">
      <div className="flex items-center justify-between">
        <span className="font-medium">🛠 {t('chat.tools.title')}</span>
        <label className="flex items-center gap-1.5 text-xs text-ink-muted">
          <input
            type="checkbox"
            checked={p.autoTools}
            onChange={(e) => p.setAutoTools(e.target.checked)}
          />
          {t('chat.tools.auto')}
        </label>
      </div>
      <p className="text-xs text-ink-muted">{t('chat.tools.lead')}</p>
      <ul className="space-y-1.5">
        {p.availableTools.map((tool) => (
          <li key={tool.name}>
            <label className="flex cursor-pointer items-start gap-2 rounded-rw border border-line bg-surface px-3 py-2 hover:border-action">
              <input
                type="checkbox"
                className="mt-0.5"
                checked={p.selectedTools.includes(tool.name)}
                onChange={(e) =>
                  p.setSelectedTools((cur) =>
                    e.target.checked ? [...cur, tool.name] : cur.filter((n) => n !== tool.name),
                  )
                }
              />
              <span className="min-w-0">
                <span className="font-medium">
                  {tool.label}
                  {tool.builtin && (
                    <span
                      className="ml-1.5 rounded-full bg-action-soft px-1.5 text-[10px] text-ink-muted"
                      title={t('chat.tools.builtinHint')}
                    >
                      {t('chat.tools.builtin')}
                    </span>
                  )}
                </span>
                <span className="block text-xs text-ink-muted">{tool.description}</span>
              </span>
            </label>
          </li>
        ))}
      </ul>
      <div className="border-t border-line pt-2">
        <span className="text-xs font-medium text-ink-muted">{t('chat.mcp.title')}</span>
        <ul className="mt-1.5 space-y-1.5">
          {p.mcpServers.map((srv) => (
            <li key={srv.id}>
              <label className="group flex cursor-pointer items-start gap-2 rounded-rw border border-line bg-surface px-3 py-2 hover:border-action">
                <input
                  type="checkbox"
                  className="mt-0.5"
                  checked={p.selectedMcp.includes(srv.id)}
                  onChange={(e) =>
                    p.setSelectedMcp((cur) =>
                      e.target.checked ? [...cur, srv.id] : cur.filter((x) => x !== srv.id),
                    )
                  }
                />
                <span className="min-w-0 flex-1">
                  <span className="font-medium">
                    {srv.label}
                    <span className="ml-1.5 rounded-full bg-band-chip/20 px-1.5 text-[10px] text-ink-muted">
                      MCP
                    </span>
                  </span>
                  <span className="block truncate text-xs text-ink-muted">{srv.url}</span>
                </span>
                <button
                  type="button"
                  onClick={(e) => {
                    e.preventDefault()
                    p.removeMcpServer(srv.id)
                  }}
                  className="invisible px-1 text-xs text-ink-muted hover:text-primary-strong group-hover:visible"
                  aria-label="delete mcp server"
                >
                  ✕
                </button>
              </label>
            </li>
          ))}
        </ul>
        <div className="mt-2 flex flex-wrap items-center gap-1.5 text-xs">
          <span className="text-ink-muted">{t('chat.mcp.presets')}:</span>
          {MCP_PRESETS.filter((pr) => !p.mcpServers.some((srv) => srv.url === pr.url)).map((pr) => (
            <button
              key={pr.url}
              type="button"
              title={pr.desc}
              onClick={() => {
                p.setMcpLabel(pr.label)
                p.setMcpUrl(pr.url)
              }}
              className="rounded-full border border-line px-2.5 py-1 hover:border-action hover:text-action"
            >
              ＋ {pr.label}
            </button>
          ))}
        </div>
        <div className="mt-2 flex flex-wrap gap-1.5">
          <input
            value={p.mcpLabel}
            onChange={(e) => p.setMcpLabel(e.target.value)}
            placeholder={t('chat.mcp.label')}
            className="w-28 rounded-rw border border-line bg-surface px-2 py-1 text-xs outline-none focus:border-action"
          />
          <input
            value={p.mcpUrl}
            onChange={(e) => p.setMcpUrl(e.target.value)}
            placeholder="https://.../mcp"
            className="min-w-40 flex-1 rounded-rw border border-line bg-surface px-2 py-1 text-xs outline-none focus:border-action"
          />
          <button
            type="button"
            onClick={() => p.addMcpServer()}
            disabled={!p.mcpLabel.trim() || !p.mcpUrl.trim()}
            className="rounded-rw border border-line px-2 py-1 text-xs hover:border-action hover:text-action disabled:opacity-40"
          >
            ＋ {t('chat.mcp.add')}
          </button>
        </div>
        {p.mcpError && <p className="mt-1 text-xs text-primary-strong">⚠ {p.mcpError}</p>}
      </div>
    </div>
  )
}

/** 生成パラメータ設定パネル(temperature/top_p/max_tokens/effort/systemPrompt/preset)。 */
function SettingsPanel(p: ComposerProps) {
  const { t } = usePrefs()
  return (
    <div className="mx-auto mb-3 max-w-3xl space-y-3 rounded-rw border border-line bg-bg p-4 text-sm">
      <div className="flex items-center gap-3">
        <span className="w-32 shrink-0 font-medium">
          {t('chat.temperature')}: {p.temperature.toFixed(1)}
        </span>
        <input
          type="range"
          min={0}
          max={1.5}
          step={0.1}
          value={p.temperature}
          onChange={(e) => p.setTemperature(Number(e.target.value))}
          className="flex-1 accent-(--rw-action)"
        />
      </div>
      <div className="flex items-center gap-3">
        <span className="w-32 shrink-0 font-medium">
          top_p: {p.topP < 1 ? p.topP.toFixed(2) : t('chat.modelDefault')}
        </span>
        <input
          type="range"
          min={0.05}
          max={1}
          step={0.05}
          value={p.topP}
          onChange={(e) => p.setTopP(Number(e.target.value))}
          className="flex-1 accent-(--rw-action)"
        />
      </div>
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
        <label className="flex items-center gap-2">
          <span className="font-medium">{t('chat.maxTokens')}</span>
          <input
            type="number"
            min={p.minMaxTokens}
            max={32768}
            value={p.maxTokens}
            onChange={(e) => p.setMaxTokens(e.target.value)}
            placeholder={t('chat.maxTokens.ph')}
            className="w-28 rounded-rw border border-line bg-surface px-2 py-1 text-xs outline-none focus:border-action"
          />
          {p.minMaxTokens > 1 && (
            <span className="text-xs text-ink-muted">
              {t('chat.maxTokens.minHint')}
              {p.minMaxTokens}
            </span>
          )}
        </label>
        {p.isReasoning && (
          <label className="flex items-center gap-2">
            <span className="font-medium">{t('chat.effort')}</span>
            <select
              value={p.effort}
              onChange={(e) => p.setEffort(e.target.value)}
              className="rounded-rw border border-line bg-surface px-2 py-1 text-xs outline-none focus:border-action"
            >
              <option value="">{t('chat.modelDefault')}</option>
              <option value="low">low（速い）</option>
              <option value="medium">medium</option>
              <option value="high">high（深く考える）</option>
            </select>
          </label>
        )}
      </div>
      <div>
        <div className="mb-1 flex items-center justify-between">
          <span className="font-medium">{t('chat.systemPrompt')}</span>
          <span className="flex items-center gap-1 text-xs">
            <select
              value={p.selectedPreset}
              onChange={(e) => {
                p.setSelectedPreset(e.target.value)
                const preset = p.presets.find((x) => x.id === e.target.value)
                if (preset) p.setSystemPrompt(preset.content)
              }}
              className="rounded-rw border border-line bg-surface px-1.5 py-1 outline-none focus:border-action"
              aria-label="preset"
            >
              <option value="">{t('chat.preset')}...</option>
              {p.presets.map((preset) => (
                <option key={preset.id} value={preset.id}>
                  {preset.name}
                </option>
              ))}
            </select>
            {p.selectedPreset && (
              <button
                type="button"
                onClick={() => p.deletePreset()}
                className="rounded-rw border border-line px-2 py-1 text-ink-muted hover:text-primary-strong"
              >
                {t('chat.preset.delete')}
              </button>
            )}
          </span>
        </div>
        <textarea
          rows={2}
          value={p.systemPrompt}
          onChange={(e) => p.setSystemPrompt(e.target.value)}
          placeholder={t('chat.systemPrompt.ph')}
          className="w-full resize-y rounded-rw border border-line bg-surface px-3 py-2 text-sm outline-none focus:border-action"
        />
        <div className="mt-1 flex justify-end gap-1">
          <input
            type="text"
            value={p.presetName}
            onChange={(e) => p.setPresetName(e.target.value)}
            placeholder={t('chat.preset.name')}
            className="rounded-rw border border-line bg-surface px-2 py-1 text-xs outline-none focus:border-action"
          />
          <button
            type="button"
            onClick={() => p.savePreset()}
            disabled={!p.presetName.trim() || !p.systemPrompt.trim()}
            className="rounded-rw border border-line px-2 py-1 text-xs hover:border-action hover:text-action disabled:opacity-40"
          >
            {t('chat.preset.save')}
          </button>
        </div>
      </div>
    </div>
  )
}
