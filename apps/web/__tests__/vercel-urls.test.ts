/**
 * On Vercel the API is the `/api` service on the same domain, so the browser
 * uses the relative NEXT_PUBLIC_API_URL `/api`. A server-side fetch has no
 * origin to resolve that against; these are the two server-side callers.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'

/** The incoming request's headers; null means "not inside a request". */
const request = vi.hoisted(() => ({ headers: null as Record<string, string> | null }))
vi.mock('next/headers', () => ({
  headers: async () => {
    if (!request.headers) throw new Error('headers() called outside a request')
    return new Headers(request.headers)
  },
}))

afterEach(() => {
  request.headers = null
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

describe('server-side branding read through the address the visitor used', () => {
  it('uses the domain the request came in on, not a guessed Vercel address', async () => {
    // A custom domain that is public while the *.vercel.app addresses sit
    // behind deployment protection: guessing the latter made every read 401.
    vi.stubEnv('API_INTERNAL_URL', '')
    vi.stubEnv('NEXT_PUBLIC_API_URL', '/api')
    vi.stubEnv('VERCEL_ENV', 'production')
    vi.stubEnv('VERCEL_PROJECT_PRODUCTION_URL', 'frame-abc.vercel.app')
    vi.stubEnv('VERCEL_URL', 'frame-abc-123.vercel.app')
    request.headers = { 'x-forwarded-host': 'frame.example.com', 'x-forwarded-proto': 'https', host: 'internal' }
    const fetchMock = stubFetch()

    const { fetchServerBranding } = await import('../lib/branding-server')
    await fetchServerBranding()

    expect(fetchMock.mock.calls[0][0]).toBe('https://frame.example.com/api/instance/branding')
  })

  it('falls back to the Host header and https when there are no forwarded headers', async () => {
    vi.stubEnv('API_INTERNAL_URL', '')
    vi.stubEnv('NEXT_PUBLIC_API_URL', '/api')
    request.headers = { host: 'review.example.com' }
    const fetchMock = stubFetch()

    const { fetchServerBranding } = await import('../lib/branding-server')
    await fetchServerBranding()

    expect(fetchMock.mock.calls[0][0]).toBe('https://review.example.com/api/instance/branding')
  })

  it('still prefers an explicit API_INTERNAL_URL over the request', async () => {
    vi.stubEnv('API_INTERNAL_URL', 'http://api:8000')
    vi.stubEnv('NEXT_PUBLIC_API_URL', '/api')
    request.headers = { host: 'review.example.com' }
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
