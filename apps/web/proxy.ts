import { NextResponse } from 'next/server'
import type { NextRequest } from 'next/server'
import { withBasePath } from './lib/base-path'

const PUBLIC_ROUTES = ['/login', '/setup']
// /botid/ is called from the login and share pages, before anyone is signed in.
// BOTID_PROXY_PREFIX is where BotID's own script and requests are served
// (rewritten to Vercel by withBotId in next.config.js); a redirect to /login
// there would break the check on the login page itself.
export const BOTID_PROXY_PREFIX = '/149e9513-01fa-4fb0-aad4-566afd725d1b/'
const PUBLIC_PREFIXES = ['/invite/', '/share/', '/botid/', BOTID_PROXY_PREFIX]

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

function isPublicRoute(pathname: string): boolean {
  if (PUBLIC_ROUTES.includes(pathname)) return true
  if (PUBLIC_PREFIXES.some((prefix) => pathname.startsWith(prefix))) return true
  return false
}

export async function proxy(request: NextRequest) {
  const { pathname } = request.nextUrl

  // Always allow public routes
  if (isPublicRoute(pathname)) {
    return NextResponse.next()
  }

  // Check if setup is needed — redirect to /setup if no superadmin exists
  // Uses a cookie cache to avoid calling the API on every request
  const setupDone = request.cookies.get('ff_setup_done')?.value
  if (!setupDone) {
    try {
      // A relative API_URL (`/api`, as on Vercel or behind Traefik) has no
      // origin on the server; resolve it against the request's own.
      const res = await fetch(new URL(`${API_URL}/setup/status`, request.url), {
        cache: 'no-store', // the ff_setup_done cookie is the cache
      })
      if (res.ok) {
        const data = await res.json()
        if (data.needs_setup) {
          return NextResponse.redirect(new URL(withBasePath('/setup'), request.url))
        }
        // Setup is done — set cookie so we don't check again
        const response = NextResponse.next()
        response.cookies.set('ff_setup_done', '1', { path: '/', maxAge: 60 * 60 * 24 }) // 24 hours
        return response
      }
    } catch {
      // API unreachable — let the request through, the page will show errors
    }
  }

  // Check for auth tokens
  const accessToken = request.cookies.get('ff_access_token')?.value
  const refreshToken = request.cookies.get('ff_refresh_token')?.value

  if (!accessToken && !refreshToken) {
    const loginUrl = new URL(withBasePath('/login'), request.url)
    loginUrl.searchParams.set('from', pathname)
    return NextResponse.redirect(loginUrl)
  }

  return NextResponse.next()
}

export const config = {
  matcher: [
    /*
     * Match all paths except:
     * - _next/static (static files)
     * - _next/image (image optimization)
     * - favicon.ico
     * - api routes
     * - public assets (images, fonts, etc.)
     */
    '/((?!_next/static|_next/image|favicon\\.ico|api/|.*\\.(?:svg|png|jpg|jpeg|gif|webp|ico|woff|woff2|ttf|otf)).*)',
  ],
}
