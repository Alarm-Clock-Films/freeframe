/**
 * A returning visitor's saved branding counts as known once the server filled it in.
 *
 * The saved copy lets a page that the server could not brand show the real
 * brand straight after hydration instead of hiding until the fetch lands. Logo
 * URLs are stable API paths, so a copy from the server stays complete however
 * old it is. A copy saved before any fetch landed holds only the defaults, and
 * one saved while logos were signed links (version 6) is discarded.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'

const KEY = 'ff-branding'

function saveVisit(minutesAgo: number | null, version = 7) {
  localStorage.setItem(KEY, JSON.stringify({
    version,
    state: {
      orgName: 'Northwind Studios',
      primaryColor: '#ed7d21',
      orgLogoDark: '/instance/branding/logo/logo_dark?v=1',
      brandingFetchedAt: minutesAgo === null ? null : Date.now() - minutesAgo * 60_000,
    },
  }))
}

async function loadStore() {
  vi.resetModules()
  const { useBrandingStore } = await import('../branding-store')
  return useBrandingStore.getState()
}

afterEach(() => localStorage.clear())

describe('the saved copy of branding', () => {
  it('counts as known when the server filled it in', async () => {
    saveVisit(5)
    const s = await loadStore()
    expect(s.cached).toBe(true)
    expect(s.orgName).toBe('Northwind Studios')
    expect(s.orgLogoDark).toBe('/instance/branding/logo/logo_dark?v=1')
  })

  it('still counts days later, since logo URLs no longer expire', async () => {
    saveVisit(3 * 24 * 60)
    const s = await loadStore()
    expect(s.cached).toBe(true)
    expect(s.orgLogoDark).toBe('/instance/branding/logo/logo_dark?v=1')
  })

  it('does not when no fetch had landed before it was saved', async () => {
    saveVisit(null)
    const s = await loadStore()
    expect(s.cached).toBe(false)
  })

  it('does not for a copy saved while logos were signed links', async () => {
    saveVisit(5, 6)
    const s = await loadStore()
    expect(s.cached).toBe(false)
    expect(s.orgLogoDark).toBeNull()
  })

  it('does not when there was no earlier visit', async () => {
    const s = await loadStore()
    expect(s.cached).toBe(false)
  })
})
