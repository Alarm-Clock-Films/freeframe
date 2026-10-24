/**
 * When nothing has said what the instance's branding is, nothing may paint
 * FreeFrame's in its place.
 *
 * The server normally supplies branding in the first byte. When it cannot (a
 * slow API, a web server that cannot reach it), the app used to paint the
 * built-in defaults -- FreeFrame's logo, name, accent and credit -- and swap in
 * the real brand once the browser's fetch landed, which reads as a flash of the
 * wrong product. It did that even for a returning visitor whose browser had the
 * real branding saved from the last visit.
 *
 * Now: a returning visitor's saved copy is used as soon as the page hydrates,
 * the accent they last saw is left in place rather than stripped back to the
 * stock colour, and with no copy at all the brand-carrying surfaces hold off
 * until branding is known.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import * as React from 'react'
import { render, screen } from '@testing-library/react'
import { renderToString } from 'react-dom/server'

import { BrandingGate, BrandingProvider, useBranding } from '../branding-provider'
import { BrandingHead } from '../branding-head'
import { PoweredByBadge } from '../powered-by-badge'
import { useBrandingStore, HARDCODED_DEFAULTS } from '@/stores/branding-store'
import { ACCENT_CACHE_KEY, accentVars } from '@/lib/accent'
import { makeInstanceBranding } from '@/test/branding-fixtures'

function Probe() {
  const b = useBranding()
  return (
    <span data-testid="probe">
      {`${b.known ? 'known' : 'unknown'}:${b.orgName}`}
    </span>
  )
}

/** What zustand's persist leaves in the store after a recent visit. */
function rehydrateRecentVisit() {
  useBrandingStore.setState({
    ...HARDCODED_DEFAULTS,
    orgName: 'Northwind Studios',
    primaryColor: '#ed7d21',
    poweredByFreeframe: false,
    brandingFetchedAt: Date.now(),
    cached: true,
  })
}

beforeEach(() => {
  useBrandingStore.setState({
    ...HARDCODED_DEFAULTS,
    loaded: false,
    loading: false,
    cached: false,
    brandingFetchedAt: null,
  })
  vi.spyOn(useBrandingStore.getState(), 'fetchBranding').mockResolvedValue(undefined)
  document.documentElement.removeAttribute('style')
  localStorage.clear()
})

describe('branding that is not known yet', () => {
  it('reports unknown when the server had none and this browser has no copy', () => {
    render(<BrandingProvider initial={null}><Probe /></BrandingProvider>)
    expect(screen.getByTestId('probe').textContent).toBe('unknown:FreeFrame')
  })

  it("is known from the server's copy", () => {
    const initial = makeInstanceBranding({ org_name: 'Northwind Studios' })
    render(<BrandingProvider initial={initial}><Probe /></BrandingProvider>)
    expect(screen.getByTestId('probe').textContent).toBe('known:Northwind Studios')
  })

  it("uses a returning visitor's saved copy once hydrated", () => {
    rehydrateRecentVisit()
    render(<BrandingProvider initial={null}><Probe /></BrandingProvider>)
    expect(screen.getByTestId('probe').textContent).toBe('known:Northwind Studios')
  })

  it('keeps the saved copy out of the server render, which has to match hydration', () => {
    rehydrateRecentVisit()
    const html = renderToString(<BrandingProvider initial={null}><Probe /></BrandingProvider>)
    expect(html).toContain('unknown:FreeFrame')
  })
})

describe('surfaces that carry the brand', () => {
  it('hides the gated screen until branding is known, then shows it', () => {
    const { container, rerender } = render(
      <BrandingProvider initial={null}><BrandingGate><p>sign in</p></BrandingGate></BrandingProvider>,
    )
    expect(container.querySelector('.invisible')).not.toBeNull()

    useBrandingStore.getState().syncBranding(makeInstanceBranding({ org_name: 'Northwind Studios' }))
    rerender(
      <BrandingProvider initial={null}><BrandingGate><p>sign in</p></BrandingGate></BrandingProvider>,
    )
    expect(container.querySelector('.invisible')).toBeNull()
  })

  it('does not flash the FreeFrame credit before knowing whether it is on', () => {
    render(<BrandingProvider initial={null}><PoweredByBadge /></BrandingProvider>)
    expect(screen.queryByText(/Powered by/)).toBeNull()
  })
})

describe('the accent while branding is unknown', () => {
  it('leaves the accent painted from the last visit in place', () => {
    // What the root layout's inline script does before first paint.
    document.documentElement.style.setProperty('--accent', '#ed7d21')
    render(<BrandingProvider initial={null}><BrandingHead /></BrandingProvider>)
    expect(document.documentElement.style.getPropertyValue('--accent')).toBe('#ed7d21')
  })

  it('saves the accent once known, for the next visit to paint early', () => {
    useBrandingStore.getState().syncBranding(makeInstanceBranding({ primary_color: '#ed7d21' }))
    render(<BrandingProvider initial={null}><BrandingHead /></BrandingProvider>)
    expect(JSON.parse(localStorage.getItem(ACCENT_CACHE_KEY) || 'null')).toEqual(accentVars('#ed7d21'))
  })

  it('forgets the saved accent once the instance has none', () => {
    localStorage.setItem(ACCENT_CACHE_KEY, JSON.stringify(accentVars('#ed7d21')))
    useBrandingStore.getState().syncBranding(makeInstanceBranding({ primary_color: null }))
    render(<BrandingProvider initial={null}><BrandingHead /></BrandingProvider>)
    expect(localStorage.getItem(ACCENT_CACHE_KEY)).toBeNull()
  })
})
