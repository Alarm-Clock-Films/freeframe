import * as React from 'react'
import { headers } from 'next/headers'
import type { InstanceBranding } from '@/stores/branding-store'

/**
 * Instance branding, read on the server so the first byte already carries it.
 *
 * Branding used to be fetched only from the browser, after mount. Until that
 * round trip landed the whole app rendered the built-in defaults, so a client
 * opening a white-labelled instance saw the FreeFrame wordmark, the FreeFrame
 * name and `<title>FreeFrame</title>` first and their own brand a moment later.
 * On a deployment far from the viewer that moment is long enough to read.
 *
 * The URL here is NOT `NEXT_PUBLIC_API_URL`. That one is written for the
 * browser -- a public domain, or a LAN address -- and inside the web container
 * `localhost:8000` is the web container itself, not the API. `API_INTERNAL_URL`
 * is the server-to-server address (`http://api:8000` under compose). It falls
 * back to the public one so a deployment that has not set it, and any setup
 * where both happen to be the same host, keeps working.
 */
/**
 * On Vercel, NEXT_PUBLIC_API_URL is the relative `/api`, which a server-side
 * fetch cannot resolve on its own. The API answers on the same domain, so the
 * read goes to the address the visitor used: that one is public and reachable
 * by definition, since the request for this page just came through it.
 *
 * Guessing the address from Vercel's variables instead was unreliable. They
 * can name a `*.vercel.app` address that sits behind Vercel's deployment
 * protection while the custom domain is public, and then every read came back
 * 401. A failed read is never cached, so it failed on every page load, and the
 * sign-in screen stayed hidden until the browser's own fetch landed.
 *
 * The Vercel address is kept only as the fallback for when there is no request
 * to read the address from.
 */
const VERCEL_HOST =
  (process.env.VERCEL_ENV === 'production' && process.env.VERCEL_PROJECT_PRODUCTION_URL) ||
  process.env.VERCEL_URL
const VERCEL_API_URL = VERCEL_HOST ? `https://${VERCEL_HOST}/api` : undefined

const PUBLIC_API_URL = process.env.NEXT_PUBLIC_API_URL

/** The scheme and host this request arrived on, or null outside a request. */
async function requestOrigin(): Promise<string | null> {
  try {
    const h = await headers()
    const host = (h.get('x-forwarded-host') ?? h.get('host'))?.split(',')[0].trim()
    if (!host) return null
    const proto = (h.get('x-forwarded-proto') ?? 'https').split(',')[0].trim()
    return `${proto}://${host}`
  } catch {
    // `next build`, tests: there is no request.
    return null
  }
}

/** Where the server reads branding from, most direct first. */
async function brandingApiBase(): Promise<string> {
  if (process.env.API_INTERNAL_URL) return process.env.API_INTERNAL_URL
  if (PUBLIC_API_URL?.startsWith('/')) {
    const origin = await requestOrigin()
    if (origin) return `${origin}${PUBLIC_API_URL}`
  }
  return VERCEL_API_URL || PUBLIC_API_URL || 'http://localhost:8000'
}

/** Long enough for a slow container start, short enough not to hold a page. */
const TIMEOUT_MS = 3000

/**
 * The uncached fetch. Exported for tests: `cache` memoises for the life of the
 * call, which would make the first result the only one a test could ever see.
 */
export async function fetchServerBranding(): Promise<InstanceBranding | null> {
    let url = '/instance/branding'
    try {
      url = `${await brandingApiBase()}/instance/branding`
      const res = await fetch(url, {
        // Revalidated rather than uncached: this is one row that changes when an
        // admin edits it, and re-fetching it on every render of every page would
        // put a round trip in front of each one. An admin's own view updates
        // immediately, since the settings screen syncs the store from its own
        // PUT response; everyone else picks the change up within the window.
        next: { revalidate: 60 },
        signal: AbortSignal.timeout(TIMEOUT_MS),
      })
      if (!res.ok) {
        // Logged because the cost is invisible otherwise: every page then
        // waits for the browser to fetch branding before showing the brand.
        console.warn(`[branding] server read got HTTP ${res.status} from ${url}`)
        return null
      }
      return (await res.json()) as InstanceBranding
    } catch (err) {
      console.warn(`[branding] server read failed for ${url}: ${err instanceof Error ? err.message : String(err)}`)
      // Never throw. This runs during `next build`, where there is no API to
      // answer, and inside a layout, where throwing would replace the whole page
      // with an error. Returning null means the client fetches branding the way
      // it always did, so the worst case is the behaviour we had before.
      return null
    }
}

/**
 * `cache` dedupes this within a single render pass, so `generateMetadata` and
 * the layout body share one request rather than issuing two.
 *
 * It only exists under React's server condition -- outside it, including under
 * the test runner, importing it gives `undefined` and calling it throws. The
 * fallback is the plain function: correct everywhere, just without the
 * deduplication, which nothing outside a server render needs.
 */
const memoize = (React as { cache?: <T>(fn: T) => T }).cache
export const getServerBranding = memoize
  ? memoize(fetchServerBranding)
  : fetchServerBranding
