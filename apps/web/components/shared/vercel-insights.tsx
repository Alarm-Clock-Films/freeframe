'use client'

/**
 * Vercel Web Analytics (page views) and Speed Insights (Core Web Vitals).
 * Rendered by the root layout on Vercel deployments only; each also has to be
 * switched on for the project in the Vercel dashboard. Every event's URL goes
 * through redactInsightsUrl first, so share and invite tokens never leave the
 * browser.
 *
 * A client component because beforeSend is a function, which a server
 * component cannot pass down.
 */
import { Analytics } from '@vercel/analytics/next'
import { SpeedInsights } from '@vercel/speed-insights/next'
import { redactInsightsUrl } from '@/lib/insights'

export function VercelInsights() {
  return (
    <>
      <Analytics beforeSend={(event) => ({ ...event, url: redactInsightsUrl(event.url) })} />
      <SpeedInsights beforeSend={(event) => ({ ...event, url: redactInsightsUrl(event.url) })} />
    </>
  )
}
