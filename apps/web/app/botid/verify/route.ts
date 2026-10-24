/**
 * POST /botid/verify: run Vercel BotID and, for a human visitor, set the
 * short-lived verification cookie the API requires on sign-in and
 * share-password requests (apps/api/services/botid.py).
 *
 * It lives in the web app because BotID's server check, checkBotId(), only
 * runs in JavaScript, while the API is a separate Python service. It is not
 * under /api, which vercel.json routes to that service.
 *
 * Called by lib/botid.ts right before a protected request; BotID protects
 * this path on the client (instrumentation-client.ts).
 */
import { checkBotId } from 'botid/server'
import { hasVerificationCookie, issueVerificationCookie, signingKey } from '@/lib/botid-cookie'

export const runtime = 'nodejs'
export const dynamic = 'force-dynamic'

const headers = { 'Cache-Control': 'private, no-store' }

export async function POST(request: Request) {
  if (request.headers.get('origin') !== new URL(request.url).origin) {
    return new Response('Forbidden', { status: 403, headers })
  }

  try {
    const key = signingKey()
    // BotID is off (Docker, local development): nothing to verify, and the
    // API does not ask for the cookie either.
    if (!key) return new Response(null, { status: 204, headers })

    // A still-valid cookie is enough; it is not renewed, so it expires on time.
    if (await hasVerificationCookie(key)) return new Response(null, { status: 204, headers })

    const result = await checkBotId({ advancedOptions: { checkLevel: 'basic' } })
    // Verified bots (search crawlers and the like) have no business signing in,
    // and a bypassed result means no check ran.
    if (!result.isHuman || result.isBot || result.isVerifiedBot || result.bypassed) {
      return new Response('Verification failed', { status: 403, headers })
    }

    await issueVerificationCookie(key)
    return new Response(null, { status: 204, headers })
  } catch (error) {
    console.error('BotID verification failed', error)
    return new Response('Verification unavailable', { status: 503, headers })
  }
}
