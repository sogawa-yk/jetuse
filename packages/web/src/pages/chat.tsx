/** チャットUI(CHAT-03): SSEストリーミング表示・中断・再生成・コピー・Markdown
 *  コンテナ。状態の保持とローダ/エフェクト/サイドバーを担い、ストリーミングは
 *  useChatStream、会話履歴は useConversations、描画は MessageList/Composer に委譲する
 *  (review-validation.md §5: 巨大ファイル分割)。 */
import { useEffect, useRef, useState, type WheelEvent } from 'react'
import { useSearchParams } from 'react-router-dom'
import { authHeaders, useUser } from '../auth'
import { PageBand } from '../components/layout'
import { usePrefs } from '../prefs'
import { Composer } from './chat/Composer'
import { MessageList } from './chat/MessageList'
import { useChatStream } from './chat/useChatStream'
import { useConversations } from './chat/useConversations'
import type { AgentDef, ModelInfo, Preset } from './chat/types'

export default function Chat() {
  const { t } = usePrefs()
  const user = useUser()
  const [searchParams, setSearchParams] = useSearchParams()
  const agentId = searchParams.get('agent')
  const [agentDef, setAgentDef] = useState<AgentDef | null>(null)
  // 既存カスタムエージェントの選択(feedback 2026-06-15): チャットから切替できる
  const [agentList, setAgentList] = useState<
    { id: string; name: string; icon?: string | null }[]
  >([])
  const [models, setModels] = useState<ModelInfo[]>([])
  const [model, setModel] = useState('gpt-oss-120b')
  // OCR画面からの引き継ぎ(ENH-07): 抽出テキストを入力欄へプリフィル(初期値で取り込み)
  const [input, setInput] = useState(() => {
    const handoff = sessionStorage.getItem('ocr.toChat')
    if (handoff) sessionStorage.removeItem('ocr.toChat')
    return handoff ?? ''
  })
  const [images, setImages] = useState<string[]>([]) // 添付画像(MM-01、data URI)
  const [notice, setNotice] = useState<string | null>(null)
  // エージェントモード(AGT-01/01b): 🛠パネルでツールをチェック選択
  const [showTools, setShowTools] = useState(false)
  const [availableTools, setAvailableTools] = useState<
    { name: string; label: string; description: string; builtin: boolean }[]
  >([])
  const [selectedTools, setSelectedTools] = useState<string[]>([])
  const [mcpServers, setMcpServers] = useState<
    { id: string; label: string; url: string }[]
  >([])
  const [selectedMcp, setSelectedMcp] = useState<string[]>([])
  const [mcpLabel, setMcpLabel] = useState('')
  const [mcpUrl, setMcpUrl] = useState('')
  const [mcpError, setMcpError] = useState<string | null>(null)
  const [autoTools, setAutoTools] = useState(false)
  const [query, setQuery] = useState('')
  // 履歴サイドバー: モバイルは初期クローズ+オーバーレイ
  const [sideOpen, setSideOpen] = useState(
    () => window.matchMedia('(min-width: 768px)').matches,
  )
  const [showSettings, setShowSettings] = useState(false)
  const [temperature, setTemperature] = useState(0.7)
  // 生成パラメータ拡張(CHAT-04b)。top_p=1は「モデル既定」扱いで送信しない
  const [topP, setTopP] = useState(1)
  const [maxTokens, setMaxTokens] = useState('')
  const [effort, setEffort] = useState('')
  const [systemPrompt, setSystemPrompt] = useState('')
  const [presets, setPresets] = useState<Preset[]>([])
  const [presetName, setPresetName] = useState('')
  const [selectedPreset, setSelectedPreset] = useState('')
  const bottomRef = useRef<HTMLDivElement>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const taRef = useRef<HTMLTextAreaElement>(null) // 入力欄の自動高さ調整
  // 自動スクロール追従(CHAT-03c): 最下部付近にいる時のみ追従。上に離れたら解除
  const followRef = useRef(true)

  const modelInfo = models.find((m) => m.key === model)
  const isReasoning = modelInfo?.reasoning ?? false
  const minMaxTokens = modelInfo?.min_max_tokens ?? 1
  const isVision = modelInfo?.vision ?? false
  // 単一画像のみのモデル(llama-3.2-vision)は添付を1枚に制限(ENH-09)
  const maxImages = modelInfo?.multi_image ? 4 : 1

  const loadPresets = () =>
    fetch('/api/presets', { headers: authHeaders(user) })
      .then((r) => r.json())
      .then((d) => setPresets(d.presets))
      .catch(() => setPresets([]))

  const loadMcpServers = () =>
    fetch('/api/agent/mcp-servers', { headers: authHeaders(user) })
      .then((r) => r.json())
      .then((d) => setMcpServers(d.servers ?? []))
      .catch(() => setMcpServers([]))

  const chat = useChatStream({
    user,
    t,
    loadConvs: () => void convo.loadConvs(),
    getConfig: () => ({
      model,
      systemPrompt,
      temperature,
      topP,
      maxTokens,
      effort,
      isReasoning,
      agentDefId: agentDef?.id ?? null,
      selectedTools,
      selectedMcp,
      autoTools,
    }),
    onStreamStart: () => {
      followRef.current = true // 送信時は追従を再開
    },
  })
  const { msgs, setMsgs, busy, pendingCalls, approving } = chat

  const convo = useConversations({
    user,
    busy,
    setMsgs,
    setModel,
    onSelect: () => {
      if (!window.matchMedia('(min-width: 768px)').matches) setSideOpen(false)
    },
  })
  const { convs, currentId, setCurrentId } = convo

  useEffect(() => {
    fetch('/api/chat/models', { headers: authHeaders(user) })
      .then((r) => r.json())
      .then((d) => setModels(d.models))
      .catch(() => setModels([]))
    void convo.loadConvs()
    void loadPresets()
    fetch('/api/agent/tools', { headers: authHeaders(user) })
      .then((r) => r.json())
      .then((d) => setAvailableTools(d.tools ?? []))
      .catch(() => setAvailableTools([]))
    void loadMcpServers()
    fetch('/api/agents', { headers: authHeaders(user) })
      .then((r) => r.json())
      .then((d) => setAgentList(d.agents ?? []))
      .catch(() => setAgentList([]))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user])

  // エージェント切替: URLの ?agent= を更新し会話をリセット
  const switchAgent = (id: string) => {
    if (busy) return
    setMsgs([])
    setCurrentId(null)
    setSearchParams(id ? { agent: id } : {})
  }

  useEffect(() => {
    let alive = true
    if (!agentId) {
      // 同期setStateを避けるためマイクロタスクで解除
      void Promise.resolve().then(() => alive && setAgentDef(null))
      return () => {
        alive = false
      }
    }
    fetch(`/api/agents/${agentId}`, { headers: authHeaders(user) })
      .then((r) => (r.ok ? r.json() : null))
      .then((a) => alive && setAgentDef(a))
      .catch(() => alive && setAgentDef(null))
    return () => {
      alive = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId])

  const addMcpServer = async () => {
    if (!mcpLabel.trim() || !mcpUrl.trim()) return
    setMcpError(null)
    const res = await fetch('/api/agent/mcp-servers', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders(user) },
      body: JSON.stringify({ label: mcpLabel.trim(), url: mcpUrl.trim() }),
    })
    const data = await res.json()
    if (!res.ok) {
      setMcpError(typeof data.detail === 'string' ? data.detail : `HTTP ${res.status}`)
      return
    }
    setMcpLabel('')
    setMcpUrl('')
    void loadMcpServers()
  }

  const removeMcpServer = async (id: string) => {
    await fetch(`/api/agent/mcp-servers/${id}`, {
      method: 'DELETE',
      headers: authHeaders(user),
    })
    setSelectedMcp((cur) => cur.filter((x) => x !== id))
    void loadMcpServers()
  }

  const savePreset = async () => {
    const name = presetName.trim()
    if (!name || !systemPrompt.trim()) return
    const res = await fetch('/api/presets', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders(user) },
      body: JSON.stringify({ name, content: systemPrompt.trim() }),
    })
    if (res.ok) {
      setPresetName('')
      void loadPresets()
    }
  }

  const deletePreset = async () => {
    if (!selectedPreset) return
    await fetch(`/api/presets/${selectedPreset}`, {
      method: 'DELETE',
      headers: authHeaders(user),
    })
    setSelectedPreset('')
    void loadPresets()
  }

  // 画像添付(MM-01): 長辺1024pxへ縮小しJPEG data URI化(帯域・トークン節約)
  const addImages = async (files: FileList | null) => {
    if (!files) return
    const added: string[] = []
    for (const file of Array.from(files)) {
      if (!file.type.startsWith('image/')) continue
      const uri = await new Promise<string | null>((resolve) => {
        const img = new Image()
        const url = URL.createObjectURL(file)
        img.onload = () => {
          const scale = Math.min(1, 1024 / Math.max(img.width, img.height))
          const canvas = document.createElement('canvas')
          canvas.width = Math.round(img.width * scale)
          canvas.height = Math.round(img.height * scale)
          canvas.getContext('2d')!.drawImage(img, 0, 0, canvas.width, canvas.height)
          URL.revokeObjectURL(url)
          resolve(canvas.toDataURL('image/jpeg', 0.85))
        }
        img.onerror = () => {
          URL.revokeObjectURL(url)
          resolve(null)
        }
        img.src = url
      })
      if (uri) added.push(uri)
    }
    setImages((cur) => [...cur, ...added].slice(0, maxImages))
  }

  const newChat = () => {
    convo.newChat()
  }

  useEffect(() => {
    // 追従は即時(瞬間)スクロール。smoothだとアニメ中に追従が維持され抜けにくいため
    if (!followRef.current) return
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [msgs])

  // 入力に応じて高さを自動調整(下限=複数行・上限192px)。送信で空になれば下限へ戻る
  useEffect(() => {
    const el = taRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 192)}px`
  }, [input])

  const onScroll = () => {
    const el = scrollRef.current
    // 下端付近(32px以内)のときだけ追従を維持/再開。少し上にスクロールすれば即解除
    if (el) followRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 32
  }

  // 上方向のホイール/トラックパッド操作で即座に追従解除(下端付近でも抜けられる)
  const onWheel = (e: WheelEvent) => {
    if (e.deltaY < 0) followRef.current = false
  }

  const send = async () => {
    const text = input.trim()
    if (!text || busy) return
    const sendImages = isVision && images.length > 0 ? images : null
    setInput('')
    setImages([])
    chat.resetTurn() // 新しいターン: ツール結果の累積をリセット
    let cid = currentId
    if (!cid) {
      // 初回送信時に会話を作成(タイトル=先頭30字)。DB障害時はハングさせず
      // ステートレスで継続し、保存されない旨を通知する(CHAT-07)
      cid = await convo.createConversation(model, text.slice(0, 30))
      setNotice(cid ? null : t('chat.dbDown'))
    }
    void chat.stream(
      [...msgs, { role: 'user', content: text, images: sendImages ?? undefined }],
      cid,
      true,
      null,
      sendImages,
    )
  }

  return (
    <div className="flex h-full flex-col">
      <PageBand icon="chat" title={t('chat.title')} helpKey="chat" />

      <div className="flex min-h-0 flex-1">
        {/* 会話履歴サイドバー(CHAT-02)。モバイルはオーバーレイドロワー */}
        {sideOpen && (
          <div
            className="fixed inset-x-0 bottom-0 top-[53px] z-30 bg-black/30 md:hidden"
            onClick={() => setSideOpen(false)}
          />
        )}
        {sideOpen && (
          <aside className="flex w-60 shrink-0 flex-col border-r border-line bg-surface max-md:fixed max-md:bottom-0 max-md:left-0 max-md:top-[53px] max-md:z-40 max-md:shadow-lg">
          {/* ヘッダ: タイトル + 閉じるボタン(履歴の場所に隣接させる) */}
          <div className="flex items-center justify-between border-b border-line px-3 py-2">
            <span className="text-sm font-semibold text-ink">{t('chat.history')}</span>
            <button
              onClick={() => setSideOpen(false)}
              aria-label={t('chat.history.hide')}
              title={t('chat.history.hide')}
              className="rounded-rw border border-line px-2 py-0.5 text-sm text-ink-muted hover:border-action hover:bg-bg hover:text-action"
            >
              ◀
            </button>
          </div>
          <div className="p-2">
            <button
              onClick={newChat}
              disabled={busy}
              className="w-full rounded-rw border border-line px-3 py-1.5 text-left text-sm hover:border-action hover:text-action disabled:opacity-40"
            >
              {t('chat.new')}
            </button>
          </div>
          <div className="px-2 pb-2">
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={t('chat.search')}
              className="w-full rounded-rw border border-line bg-bg px-2 py-1 text-xs outline-none focus:border-action"
            />
          </div>
          <ul className="min-h-0 flex-1 space-y-0.5 overflow-y-auto px-2 pb-2">
            {convs
              .filter((c) => !query.trim() || (c.title ?? '').includes(query.trim()))
              .map((c) => (
              <li key={c.id} className="group flex items-center">
                <button
                  onClick={() => void convo.selectConv(c.id)}
                  className={`min-w-0 flex-1 truncate rounded-rw px-2 py-1.5 text-left text-sm ${
                    c.id === currentId
                      ? 'bg-action-soft font-medium text-ink'
                      : 'text-ink-muted hover:bg-bg hover:text-ink'
                  }`}
                  title={c.title ?? ''}
                >
                  {c.title || '(無題)'}
                </button>
                <button
                  onClick={() => void convo.deleteConv(c.id)}
                  className="invisible px-1.5 text-xs text-ink-muted hover:text-primary-strong group-hover:visible"
                  aria-label="delete conversation"
                >
                  ✕
                </button>
              </li>
              ))}
          </ul>
          </aside>
        )}

        <div className="flex min-w-0 flex-1 flex-col">
          {/* 履歴が閉じている時の開くボタン。履歴が出る左上に高コントラストで配置 */}
          {!sideOpen && (
            <div className="border-b border-line bg-surface px-3 py-2">
              <button
                onClick={() => setSideOpen(true)}
                aria-label={t('chat.history.show')}
                className="rounded-rw border border-action bg-action-soft px-3 py-1.5 text-sm font-medium text-ink hover:bg-action hover:text-white"
              >
                ☰ {t('chat.history.show')}
              </button>
            </div>
          )}
          <div ref={scrollRef} onScroll={onScroll} onWheel={onWheel} className="flex-1 overflow-y-auto px-4 py-4">
            <MessageList
              msgs={msgs}
              busy={busy}
              pendingCalls={pendingCalls}
              approving={approving}
              bottomRef={bottomRef}
              onCopy={(text) => navigator.clipboard.writeText(text)}
              onRegenerate={() => chat.regenerate(currentId)}
              onApprove={() => void chat.approveTools(currentId)}
              onDeny={() => chat.denyTools(currentId)}
            />
          </div>

          <Composer
            agentDef={agentDef}
            agentList={agentList}
            agentId={agentId}
            switchAgent={switchAgent}
            notice={notice}
            setNotice={setNotice}
            showTools={showTools}
            setShowTools={setShowTools}
            autoTools={autoTools}
            setAutoTools={setAutoTools}
            availableTools={availableTools}
            selectedTools={selectedTools}
            setSelectedTools={setSelectedTools}
            mcpServers={mcpServers}
            selectedMcp={selectedMcp}
            setSelectedMcp={setSelectedMcp}
            mcpLabel={mcpLabel}
            setMcpLabel={setMcpLabel}
            mcpUrl={mcpUrl}
            setMcpUrl={setMcpUrl}
            mcpError={mcpError}
            addMcpServer={() => void addMcpServer()}
            removeMcpServer={(id) => void removeMcpServer(id)}
            showSettings={showSettings}
            setShowSettings={setShowSettings}
            temperature={temperature}
            setTemperature={setTemperature}
            topP={topP}
            setTopP={setTopP}
            maxTokens={maxTokens}
            setMaxTokens={setMaxTokens}
            minMaxTokens={minMaxTokens}
            isReasoning={isReasoning}
            effort={effort}
            setEffort={setEffort}
            systemPrompt={systemPrompt}
            setSystemPrompt={setSystemPrompt}
            presets={presets}
            presetName={presetName}
            setPresetName={setPresetName}
            selectedPreset={selectedPreset}
            setSelectedPreset={setSelectedPreset}
            savePreset={() => void savePreset()}
            deletePreset={() => void deletePreset()}
            images={images}
            setImages={setImages}
            isVision={isVision}
            addImages={(files) => void addImages(files)}
            model={model}
            setModel={setModel}
            models={models}
            input={input}
            setInput={setInput}
            taRef={taRef}
            busy={busy}
            send={() => void send()}
            stop={chat.stop}
          />
        </div>
      </div>
    </div>
  )
}
