/**
 * A returning visitor's saved branding counts as known only while it is fresh.
 *
 * The saved copy lets a page that the server could not brand show the real
 * brand straight after hydration instead of hiding until the fetch lands. Its
 * logo URLs are signed for an hour, though, and past the margin they are
 * dropped; treating that copy as complete would paint the FreeFrame mark where
 * the real logo goes. So `cached` is set only for a recent copy.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'

const KEY = 'ff-branding'

function saveVisit(minutesAgo: number) {
  localStorage.setItem(KEY, JSON.stringify({
    version: 6,
    state: {
      orgName: 'Northwind Studios',
      primaryColor: '#ed7d21',
      orgLogoDark: 'https://cdn.example/dark.png',
      brandingFetchedAt: Date.now() - minutesAgo * 60_000,
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
  it('counts as known when it is recent', async () => {
    saveVisit(5)
    const s = await loadStore()
    expect(s.cached).toBe(true)
    expect(s.orgName).toBe('Northwind Studios')
    expect(s.orgLogoDark).toBe('https://cdn.example/dark.png')
  })

  it('does not once its logo URLs may have expired', async () => {
    saveVisit(50)
    const s = await loadStore()
    expect(s.cached).toBe(false)
    expect(s.orgLogoDark).toBeNull()
  })

  it('does not when there was no earlier visit', async () => {
    const s = await loadStore()
    expect(s.cached).toBe(false)
  })
})
