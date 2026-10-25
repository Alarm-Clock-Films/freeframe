/**
 * Mount the app under a sub-path (e.g. /freeframe) instead of the domain root.
 * Build-time: set NEXT_PUBLIC_BASE_PATH and rebuild the web image. Empty by
 * default, so root deployments are unchanged.
 */
const basePath = process.env.NEXT_PUBLIC_BASE_PATH || ''

/**
 * On Vercel the API is a second service on the same domain, mounted at /api
 * (see vercel.json), so the browser reaches it with a relative URL. An explicit
 * NEXT_PUBLIC_API_URL still wins. Vercel sets VERCEL=1 during its builds.
 */
const onVercel = process.env.VERCEL === '1'
const env = {}
if (onVercel && !process.env.NEXT_PUBLIC_API_URL) {
  env.NEXT_PUBLIC_API_URL = '/api'
}
// Vercel BotID works only on Vercel (lib/botid.ts). The API enforces it only
// when BOTID_COOKIE_SECRET is set, so this alone changes nothing for sign-in.
if (onVercel) {
  env.NEXT_PUBLIC_BOTID = '1'
}

/**
 * Security headers on every page. The API sets its own
 * (apps/api/middleware/security_headers.py).
 *
 * Framing is limited to this origin rather than denied: nothing embeds
 * FreeFrame elsewhere, and same-origin frames keep working if a view uses one.
 * There is no full Content-Security-Policy: the player loads media from the
 * storage bucket and HLS through the API, and a policy that misses one of
 * those breaks playback. frame-ancestors is the part that stops clickjacking.
 * Vercel adds Strict-Transport-Security on its own domains and on custom
 * domains it serves.
 */
const securityHeaders = [
  { key: 'X-Frame-Options', value: 'SAMEORIGIN' },
  { key: 'Content-Security-Policy', value: "frame-ancestors 'self'" },
  { key: 'X-Content-Type-Options', value: 'nosniff' },
  { key: 'Referrer-Policy', value: 'strict-origin-when-cross-origin' },
  // Features FreeFrame never uses. Clipboard and fullscreen stay allowed.
  { key: 'Permissions-Policy', value: 'camera=(), microphone=(), geolocation=(), payment=(), usb=()' },
]

/**
 * FreeFrame is a private workspace: sign-in pages, projects and share links
 * should never show up in search results. This header covers every response
 * the web app serves, including the share pages (anyone can reach those) and
 * deployments under NEXT_PUBLIC_BASE_PATH, where a crawler never reads the
 * sub-path robots.txt. The root layout sets the same rule as a <meta> tag and
 * app/robots.ts asks crawlers to stay out. The API sends it too
 * (apps/api/middleware/security_headers.py).
 */
const noIndexHeaders = [{ key: 'X-Robots-Tag', value: 'noindex, nofollow' }]

/** @type {import('next').NextConfig} */
const nextConfig = {
  basePath,
  env,
  async headers() {
    return [{ source: '/:path*', headers: [...securityHeaders, ...noIndexHeaders] }]
  },
  // The Docker images run the standalone server; Vercel builds its own output.
  output: onVercel ? undefined : 'standalone',
  images: {
    remotePatterns: [
      {
        protocol: 'http',
        hostname: 'localhost',
        port: '9000',
      },
    ],
  },
}

// BotID's browser script and its requests are served from this domain and
// proxied to Vercel; withBotId adds those rewrites. Without them the script
// 404s, BotID cannot classify anyone as a person, and with
// BOTID_COOKIE_SECRET set every sign-in would be refused. Vercel only: Docker
// and local builds never load BotID (lib/botid.ts).
module.exports = onVercel ? require('botid/next/config').withBotId(nextConfig) : nextConfig
