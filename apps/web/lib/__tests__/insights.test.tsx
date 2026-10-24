/**
 * Vercel Web Analytics and Speed Insights record every page's URL. Share and
 * invite links carry access tokens in the path, so those must never be sent.
 */
import { describe, expect, it, vi } from 'vitest'
import { redactInsightsUrl, vercelInsightsEnabled } from '../insights'

describe('redactInsightsUrl', () => {
  it.each([
    ['https://frame.example.com/share/Ab3dEfGh1jK', 'https://frame.example.com/share/[token]'],
    ['https://frame.example.com/invite/9f8e7d6c5b', 'https://frame.example.com/invite/[token]'],
    // Under a base path, and with anything after the token.
    ['https://example.com/freeframe/share/Ab3dEf/extra', 'https://example.com/freeframe/share/[token]/extra'],
  ])('replaces the token in %s', (raw, expected) => {
    expect(redactInsightsUrl(raw)).toBe(expected)
  })

  it('drops query strings and fragments, which can carry tokens or paths', () => {
    expect(redactInsightsUrl('https://frame.example.com/login?from=%2Fshare%2FAb3dEf')).toBe('https://frame.example.com/login')
    expect(redactInsightsUrl('https://frame.example.com/share/Ab3dEf?asset=1#t=10')).toBe('https://frame.example.com/share/[token]')
  })

  it('leaves other pages as they are', () => {
    expect(redactInsightsUrl('https://frame.example.com/projects/0d1f')).toBe('https://frame.example.com/projects/0d1f')
    expect(redactInsightsUrl('https://frame.example.com/share')).toBe('https://frame.example.com/share')
  })

  it('handles a bare path too', () => {
    expect(redactInsightsUrl('/share/Ab3dEf?x=1')).toBe('/share/[token]')
  })
})

describe('vercelInsightsEnabled', () => {
  it('is on only where Vercel sets VERCEL=1', () => {
    expect(vercelInsightsEnabled({ VERCEL: '1' })).toBe(true)
    expect(vercelInsightsEnabled({})).toBe(false)
    expect(vercelInsightsEnabled({ VERCEL: '' })).toBe(false)
  })
})

describe('VercelInsights', () => {
  it('passes every event through the redaction', async () => {
    type Event = { type: string; url: string }
    type Hook = (e: Event) => unknown
    const seen: Record<string, Hook> = {}
    vi.doMock('@vercel/analytics/next', () => ({
      Analytics: (props: { beforeSend: Hook }) => { seen.analytics = props.beforeSend; return null },
    }))
    vi.doMock('@vercel/speed-insights/next', () => ({
      SpeedInsights: (props: { beforeSend: Hook }) => { seen.speed = props.beforeSend; return null },
    }))
    const { render } = await import('@testing-library/react')
    const { VercelInsights } = await import('../../components/shared/vercel-insights')
    render(<VercelInsights />)

    const url = 'https://frame.example.com/share/Ab3dEf?from=x'
    expect(seen.analytics({ type: 'pageview', url })).toEqual({ type: 'pageview', url: 'https://frame.example.com/share/[token]' })
    expect(seen.speed({ type: 'vital', url })).toEqual({ type: 'vital', url: 'https://frame.example.com/share/[token]' })
    vi.doUnmock('@vercel/analytics/next')
    vi.doUnmock('@vercel/speed-insights/next')
  })
})

describe('the sign-in proxy', () => {
  it("leaves Vercel's own /_vercel/ paths alone", async () => {
    const { config } = await import('../../proxy')
    const matches = (path: string) => config.matcher.some((m: string) => new RegExp(`^${m}$`).test(path))
    expect(matches('/_vercel/insights/script.js')).toBe(false)
    expect(matches('/_vercel/speed-insights/vitals')).toBe(false)
    expect(matches('/projects')).toBe(true)
  })
})
