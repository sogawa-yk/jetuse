import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { HashRouter } from 'react-router-dom'
import './theme.css'
import App from './App.tsx'
import { AuthProvider, loadAuthConfig } from './auth.tsx'
import { PrefsProvider } from './prefs.tsx'

// HashRouter採用: 静的ホスティング(API GW+Object Storage)のディープリンク404を
// ルーティング側で回避する(ADR-0004 検証3の結論)
// 認証設定(/config.json)を render 前に読み込む(INFRA-03 ORM: 実行時OIDC設定)
void loadAuthConfig().then(() => {
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <PrefsProvider>
        <AuthProvider>
          <HashRouter>
            <App />
          </HashRouter>
        </AuthProvider>
      </PrefsProvider>
    </StrictMode>,
  )
})
