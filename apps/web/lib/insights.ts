/**
 * Vercel Web Analytics and Speed Insights.
 *
 * Both record the URL of every page view. FreeFrame puts access tokens in two
 * paths, /share/<token> (a share link: anyone with it can open the content)
 * and /invite/<token> (an account invite), so those are replaced with
 * `[token]` before anything leaves the browser. Query strings and fragments
 * are dropped altogether: none is needed for page statistics, and some carry
 * paths or tokens (the sign-in redirect's `?from=`, for one).
 */

/** Path segments whose next segment is a secret token. */
const TOKEN_SEGMENTS = new Set(['share', 'invite'])

function redactPath(pathname: string): string {
  const parts = pathname.split('/')
  for (let i = 0; i < parts.length - 1; i++) {
    if (TOKEN_SEGMENTS.has(parts[i]) && parts[i + 1]) parts[i + 1] = '[token]'
  }
  return parts.join('/')
}

/** The URL with share and invite tokens replaced, and no query or fragment. */
export function redactInsightsUrl(raw: string): string {
  try {
    const url = new URL(raw)
    return `${url.origin}${redactPath(url.pathname)}`
  } catch {
    // Not an absolute URL: treat it as a path.
    return redactPath(raw.split(/[?#]/)[0])
  }
}

/**
 * Whether to load the Vercel scripts: only on Vercel, which sets VERCEL=1 at
 * build and run time. Elsewhere (Docker, local development) the
 * /_vercel/insights and /_vercel/speed-insights endpoints don't exist, and
 * loading the scripts would only add failing requests.
 */
export function vercelInsightsEnabled(env: Record<string, string | undefined> = process.env): boolean {
  return env.VERCEL === '1'
}
