/**
 * FreeFrame is a private workspace, so search engines are kept out at every
 * layer: robots.txt asks crawlers to stay away, every page sends
 * `X-Robots-Tag: noindex, nofollow`, and the root layout adds the matching
 * <meta name="robots"> tag. Share links are the pages most at risk, since
 * anyone can open them and they get pasted into public places.
 */
import { createRequire } from 'node:module'
import { afterEach, describe, expect, it, vi } from 'vitest'

const require = createRequire(import.meta.url)

vi.mock('next/font/google', () => ({
  DM_Sans: () => ({ variable: 'font-sans', className: 'font-sans' }),
}))
vi.mock('@/lib/branding-server', () => ({
  getServerBranding: async () => null,
}))

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('search engine indexing', () => {
  it('robots.txt disallows the whole site for every crawler', async () => {
    const { default: robots } = await import('../robots')
    expect(robots().rules).toEqual({ userAgent: '*', disallow: '/' })
  })

  it('every page sends X-Robots-Tag: noindex, nofollow', async () => {
    const path = require.resolve('../../next.config.js')
    delete require.cache[path]
    const rules = await require('../../next.config.js').headers()
    const everyPage = rules.find((r: { source: string }) => r.source === '/:path*')
    const tag = everyPage.headers.find((h: { key: string }) => h.key === 'X-Robots-Tag')
    expect(tag?.value).toBe('noindex, nofollow')
  })

  it('the root layout marks every page noindex, nofollow', async () => {
    const { generateMetadata } = await import('../layout')
    const metadata = await generateMetadata()
    expect(metadata.robots).toEqual({ index: false, follow: false })
  })

  it('crawlers can read robots.txt without signing in', async () => {
    const { NextRequest } = await import('next/server')
    const { proxy } = await import('../../proxy')
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ needs_setup: false }))))
    const res = await proxy(new NextRequest('https://frame.example.com/robots.txt'))
    expect(res.headers.get('location')).toBeNull()
  })
})
