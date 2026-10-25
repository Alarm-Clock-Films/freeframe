import type { MetadataRoute } from 'next'

/**
 * Served at /robots.txt. FreeFrame is a private workspace, so every crawler is
 * asked to stay out of the whole site, the API under /api included.
 *
 * robots.txt is only a request: a crawler can ignore it, or list a URL it
 * found linked elsewhere without visiting it. Every page and API response also
 * carries `X-Robots-Tag: noindex, nofollow` (next.config.js and the API's
 * security headers) and the root layout adds the matching <meta> tag, so a
 * page that does get fetched is still kept out of the index.
 *
 * Crawlers only read /robots.txt at the domain root. Under a
 * NEXT_PUBLIC_BASE_PATH this file is served below the sub-path, where they
 * never look; the header and <meta> tag still apply there.
 */
export default function robots(): MetadataRoute.Robots {
  return {
    rules: { userAgent: '*', disallow: '/' },
  }
}
