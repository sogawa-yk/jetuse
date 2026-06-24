import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { AuthProvider, loadAuthConfig, useUser } from './auth'

/** 認証の実行時設定(/config.json)読み込み(INFRA-03 ORMワンクリック対応)。 */

function Probe() {
  const u = useUser()
  return <span>user:{u.name}</span>
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('loadAuthConfig', () => {
  it('authRequired:false の config.json では dev-user モードで子を描画する', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ authRequired: false }),
    }))
    await loadAuthConfig()
    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    )
    expect(screen.getByText('user:dev-user')).toBeInTheDocument()
  })

  it('config.json 取得失敗時も dev-user にフォールバックする', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('network')))
    await loadAuthConfig()
    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    )
    expect(screen.getByText('user:dev-user')).toBeInTheDocument()
  })

  it('authRequired:true でも authority/clientId 欠落なら認証を有効化しない(dev-user)', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ authRequired: true }),
    }))
    await loadAuthConfig()
    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    )
    expect(screen.getByText('user:dev-user')).toBeInTheDocument()
  })
})
