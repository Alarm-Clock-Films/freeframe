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

/** @type {import('next').NextConfig} */
const nextConfig = {
  basePath,
  env,
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

module.exports = nextConfig
