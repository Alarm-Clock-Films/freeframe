import { createRequire } from 'node:module'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const require = createRequire(import.meta.url)

function loadNextConfig() {
  const path = require.resolve('../../next.config.js')
  delete require.cache[path]
  return require('../../next.config.js')
}

async function loadBotid() {
  vi.resetModules()
  return import('../botid')
}

const originalVercel = process.env.VERCEL

beforeEach(() => vi.restoreAllMocks())
afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  if (originalVercel === undefined) delete process.env.VERCEL
  else process.env.VERCEL = originalVercel
})

describe('confirmHuman', () => {
  it('does nothing outside Vercel', async () => {
    vi.stubEnv('NEXT_PUBLIC_BOTID', '')
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const { confirmHuman } = await loadBotid()
    await confirmHuman()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('asks /botid/verify for a cookie on Vercel', async () => {
    vi.stubEnv('NEXT_PUBLIC_BOTID', '1')
    const fetchMock = vi.fn(async () => new Response(null, { status: 204 }))
    vi.stubGlobal('fetch', fetchMock)
    const { confirmHuman } = await loadBotid()
    await confirmHuman()
    expect(fetchMock).toHaveBeenCalledWith('/botid/verify', expect.objectContaining({
      method: 'POST',
      credentials: 'same-origin',
    }))
  })

  it('never throws: the API decides and the form shows its answer', async () => {
    vi.stubEnv('NEXT_PUBLIC_BOTID', '1')
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('offline') }))
    const { confirmHuman } = await loadBotid()
    await expect(confirmHuman()).resolves.toBeUndefined()
  })
})

describe('next.config', () => {
  it('turns BotID on only in Vercel builds', () => {
    delete process.env.VERCEL
    expect(loadNextConfig().env.NEXT_PUBLIC_BOTID).toBeUndefined()
    process.env.VERCEL = '1'
    expect(loadNextConfig().env.NEXT_PUBLIC_BOTID).toBe('1')
  })

  it('sends security headers on every page', async () => {
    const rules = await loadNextConfig().headers()
    expect(rules).toHaveLength(1)
    expect(rules[0].source).toBe('/:path*')
    const headers = Object.fromEntries(rules[0].headers.map((h: { key: string; value: string }) => [h.key, h.value]))
    expect(headers['X-Frame-Options']).toBe('SAMEORIGIN')
    expect(headers['Content-Security-Policy']).toBe("frame-ancestors 'self'")
    expect(headers['X-Content-Type-Options']).toBe('nosniff')
    expect(headers['Referrer-Policy']).toBe('strict-origin-when-cross-origin')
    // The app copies share links to the clipboard and plays video full screen.
    expect(headers['Permissions-Policy']).not.toMatch(/clipboard|fullscreen/)
  })
})
