import { withBasePath } from './base-path'

/**
 * Vercel BotID runs only in Vercel deployments: next.config.js sets
 * NEXT_PUBLIC_BOTID there. Docker and local builds skip it entirely.
 */
export const BOTID_ENABLED = process.env.NEXT_PUBLIC_BOTID === '1'

/** The one path BotID protects on the client; see app/botid/verify/route.ts. */
export const BOTID_VERIFY_PATH = withBasePath('/botid/verify')

/**
 * Prove to the API that a person is making the next request.
 *
 * Call it right before a request the API guards with BotID (sign-in and
 * share passwords). It asks /botid/verify for a fresh verification cookie,
 * which the browser then sends along. Failures are swallowed on purpose: the
 * API decides, and answers a request without a valid cookie with 403
 * `botid_required`, whose message the form shows.
 */
export async function confirmHuman(): Promise<void> {
  if (!BOTID_ENABLED) return
  try {
    await fetch(BOTID_VERIFY_PATH, {
      method: 'POST',
      credentials: 'same-origin',
      cache: 'no-store',
    })
  } catch {
    // See above: the API's answer is what the user sees.
  }
}
