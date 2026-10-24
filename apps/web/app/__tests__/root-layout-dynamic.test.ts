/**
 * The root layout must render per request, not once at build.
 *
 * It reads instance branding on the server so the first paint already carries
 * the custom logo, name and accent. Nothing else in it is request-specific, so
 * without an explicit opt-out Next prerenders every static route at
 * `next build` -- where no API answers, so the HTML is baked with the FreeFrame
 * defaults and the sign-in page flashed the FreeFrame logo before the browser's
 * own fetch swapped in the real brand.
 */
import { describe, it, expect, vi } from 'vitest'

vi.mock('next/font/google', () => ({
  DM_Sans: () => ({ variable: 'font-sans', className: 'font-sans' }),
}))

describe('root layout rendering', () => {
  it('is rendered per request so server branding reaches the first paint', async () => {
    const layout = await import('../layout')
    expect(layout.dynamic).toBe('force-dynamic')
  })
})
