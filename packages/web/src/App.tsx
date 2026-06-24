import { lazy, Suspense, useEffect, useState } from 'react'
import { Outlet, Route, Routes } from 'react-router-dom'
import { loadBranding, type Branding } from './branding'
import { Shell } from './components/layout'

// ルートページは React.lazy で個別チャンク化(P2バンドル分割)。
// Shell/ナビは常時表示のため eager のまま。各ページは default export。
const AgentBuilder = lazy(() => import('./pages/agentbuilder'))
const Agents = lazy(() => import('./pages/agents'))
const Builder = lazy(() => import('./pages/builder'))
const Chat = lazy(() => import('./pages/chat'))
const DbChat = lazy(() => import('./pages/dbchat'))
const Design = lazy(() => import('./pages/design'))
const Admin = lazy(() => import('./pages/admin'))
const Home = lazy(() => import('./pages/home'))
const Minutes = lazy(() => import('./pages/minutes'))
const Ocr = lazy(() => import('./pages/ocr'))
const Realtime = lazy(() => import('./pages/realtime'))
const Video = lazy(() => import('./pages/video'))
const VoiceChat = lazy(() => import('./pages/voicechat'))
const Rag = lazy(() => import('./pages/rag'))
const Settings = lazy(() => import('./pages/settings'))
const UsecaseRun = lazy(() => import('./pages/usecase'))

/** ルート遅延ロード中の軽量フォールバック(中央スピナー) */
function PageFallback() {
  return (
    <div className="flex justify-center p-8">
      <div className="h-6 w-6 animate-spin rounded-full border-2 border-line border-t-accent" />
    </div>
  )
}

export default function App() {
  const [branding, setBranding] = useState<Branding | null>(null)

  useEffect(() => {
    loadBranding().then(setBranding)
  }, [])

  return (
    <Routes>
      <Route element={<Shell branding={branding} />}>
        <Route
          element={
            <Suspense fallback={<PageFallback />}>
              <Outlet />
            </Suspense>
          }
        >
          <Route index element={<Home />} />
          <Route path="chat" element={<Chat />} />
          <Route path="rag" element={<Rag />} />
          <Route path="dbchat" element={<DbChat />} />
          <Route path="minutes" element={<Minutes />} />
          <Route path="realtime" element={<Realtime />} />
          <Route path="voicechat" element={<VoiceChat />} />
          <Route path="video" element={<Video />} />
          <Route path="ocr" element={<Ocr />} />
          <Route path="uc/:id" element={<UsecaseRun />} />
          <Route path="builder" element={<Builder />} />
          <Route path="agents" element={<Agents />} />
          <Route path="agents/new" element={<AgentBuilder />} />
          <Route path="agents/:id" element={<AgentBuilder />} />
          <Route path="builder/:id" element={<Builder />} />
          <Route path="admin" element={<Admin />} />
          <Route path="settings" element={<Settings onBranding={setBranding} />} />
          <Route path="design" element={<Design />} />
        </Route>
      </Route>
    </Routes>
  )
}
