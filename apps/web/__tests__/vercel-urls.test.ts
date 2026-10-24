/**
 * On Vercel the API is the `/api` service on the same domain, so the browser
 * uses the relative NEXT_PUBLIC_API_URL `/api`. A server-side fetch has no
 * origin to resolve that against; these are the two server-side callers.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  vi.resetModules()
})

function stubFetch() {
  const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => ({
    ok: true,
    json: async () => ({ needs_setup: true }),
  }))
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

describe('server-side branding on Vercel', () => {
  it('reads branding from this deployment when only the relative URL is set', async () => {
    vi.stubEnv('API_INTERNAL_URL', '')
    vi.stubEnv('NEXT_PUBLIC_API_URL', '/api')
    vi.stubEnv('VERCEL_URL', 'freeframe-abc123.vercel.app')
    const fetchMock = stubFetch()

    const { fetchServerBranding } = await import('../lib/branding-server')
    await fetchServerBranding()

    expect(fetchMock.mock.calls[0][0]).toBe('https://freeframe-abc123.vercel.app/api/instance/branding')
  })

  it('uses the production domain in production, which deployment protection never blocks', async () => {
    vi.stubEnv('API_INTERNAL_URL', '')
    vi.stubEnv('NEXT_PUBLIC_API_URL', '/api')
    vi.stubEnv('VERCEL_ENV', 'production')
    vi.stubEnv('VERCEL_URL', 'freeframe-abc123.vercel.app')
    vi.stubEnv('VERCEL_PROJECT_PRODUCTION_URL', 'review.example.com')
    const fetchMock = stubFetch()

    const { fetchServerBranding } = await import('../lib/branding-server')
    await fetchServerBranding()

    expect(fetchMock.mock.calls[0][0]).toBe('https://review.example.com/api/instance/branding')
  })

  it('keeps the deployment URL on a preview', async () => {
    vi.stubEnv('API_INTERNAL_URL', '')
    vi.stubEnv('NEXT_PUBLIC_API_URL', '/api')
    vi.stubEnv('VERCEL_ENV', 'preview')
    vi.stubEnv('VERCEL_URL', 'freeframe-abc123.vercel.app')
    vi.stubEnv('VERCEL_PROJECT_PRODUCTION_URL', 'review.example.com')
    const fetchMock = stubFetch()

    const { fetchServerBranding } = await import('../lib/branding-server')
    await fetchServerBranding()

    expect(fetchMock.mock.calls[0][0]).toBe('https://freeframe-abc123.vercel.app/api/instance/branding')
  })

  it('still prefers an explicit API_INTERNAL_URL', async () => {
    vi.stubEnv('API_INTERNAL_URL', 'http://api:8000')
    vi.stubEnv('VERCEL_URL', 'freeframe-abc123.vercel.app')
    const fetchMock = stubFetch()

    const { fetchServerBranding } = await import('../lib/branding-server')
    await fetchServerBranding()

    expect(fetchMock.mock.calls[0][0]).toBe('http://api:8000/instance/branding')
  })
})

describe('first-run setup check in the proxy', () => {
  it('resolves a relative API URL against the request', async () => {
    vi.stubEnv('NEXT_PUBLIC_API_URL', '/api')
    const fetchMock = stubFetch()

    const { NextRequest } = await import('next/server')
    const { proxy } = await import('../proxy')
    const res = await proxy(new NextRequest('https://review.example.com/projects'))

    expect(String(fetchMock.mock.calls[0][0])).toBe('https://review.example.com/api/setup/status')
    expect(res.headers.get('location')).toBe('https://review.example.com/setup')
  })
})
