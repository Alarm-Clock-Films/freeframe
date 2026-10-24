/**
 * Deriving the accent CSS variables from one colour.
 *
 * Two places set an accent: instance branding (applied to the whole app) and a
 * share link's own appearance (applied only to that page, and which must win
 * there). They share this so a share link overriding the accent also overrides
 * the hover and muted shades — otherwise the button repaints but its hover
 * state stays the instance's colour.
 */

/** The tokens every accent-painted surface resolves through.
 *  --border-focus is in here because it is a pure alias of the accent in both
 *  palettes (globals.css), and it is the only colour on many neutral surfaces —
 *  the secondary button's hover border, for instance. Branding that skipped it
 *  left those surfaces on the product's stock blue. */
export const ACCENT_VAR_NAMES = ['--accent', '--accent-hover', '--accent-muted', '--border-focus', '--accent-fg'] as const

/** The two text colours an accent surface can carry. */
export const ACCENT_TEXT_DARK = '#18181b'
export const ACCENT_TEXT_LIGHT = '#ffffff'

/** The built-in accents (globals.css, dark and light). Their surfaces keep the
 *  text colour they were designed with, even when branding names one of them. */
export const STOCK_ACCENTS = ['#5b8def', '#4a7de8'] as const

export function parseHex(hex: string): [number, number, number] | null {
  const m = /^#([0-9a-f]{6})$/i.exec(hex.trim())
  if (!m) return null
  const n = parseInt(m[1], 16)
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255]
}

/** Perceived brightness, used to decide which way the hover shade moves: a dark
 *  accent reads as "pressed" when lightened, a light one when darkened. */
function luminance([r, g, b]: [number, number, number]): number {
  return (0.299 * r + 0.587 * g + 0.114 * b) / 255
}


function shift(rgb: [number, number, number], amount: number): string {
  const to = amount > 0 ? 255 : 0
  const t = Math.abs(amount)
  const [r, g, b] = rgb.map((c) => Math.round(c + (to - c) * t)) as [number, number, number]
  return `rgb(${r} ${g} ${b})`
}

/** WCAG relative luminance, 0 (black) to 1 (white). */
export function relativeLuminance([r, g, b]: [number, number, number]): number {
  const lin = (c: number) => {
    const v = c / 255
    return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4
  }
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)
}

/** WCAG contrast ratio between two 6-digit hex colours, 1 to 21. */
export function contrastRatio(a: string, b: string): number {
  const ra = parseHex(a)
  const rb = parseHex(b)
  if (!ra || !rb) return 1
  const [hi, lo] = [relativeLuminance(ra), relativeLuminance(rb)].sort((x, y) => y - x)
  return (hi + 0.05) / (lo + 0.05)
}

/** Text colour for buttons and badges painted in `color`: whichever of white
 *  and near-black has the higher WCAG contrast against it. Null for a stock
 *  accent, whose surfaces keep their own text colour, and for a value that
 *  isn't a 6-digit hex.
 *
 *  This is a contrast comparison rather than the brightness cut-off
 *  readableTextOn uses, which puts white on mid tones (orange, green, teal)
 *  where white reads at under 3:1. */
export function accentForeground(color: string): string | null {
  if (!parseHex(color)) return null
  if ((STOCK_ACCENTS as readonly string[]).includes(color.trim().toLowerCase())) return null
  return contrastRatio(color, ACCENT_TEXT_DARK) > contrastRatio(color, ACCENT_TEXT_LIGHT)
    ? ACCENT_TEXT_DARK
    : ACCENT_TEXT_LIGHT
}

/** The accent variables for a colour, or null if it isn't a 6-digit hex. */
export function accentVars(color: string | null | undefined): Record<string, string> | null {
  const rgb = color ? parseHex(color) : null
  if (!rgb || !color) return null
  return {
    '--accent': color,
    '--accent-hover': shift(rgb, luminance(rgb) > 0.6 ? -0.14 : 0.16),
    // Keep muted accents opaque. An alpha color lets the surface behind each chip
    // change its appearance; blending with the theme's secondary surface gives
    // every consumer one stable, readable color instead.
    '--accent-muted': `color-mix(in srgb, ${color} 18%, var(--bg-secondary))`,
    // Alias of the accent in both default palettes; hover and focus borders
    // resolve through it.
    '--border-focus': color,
    // Text on accent surfaces (the text-accent-fg* utilities). `initial`
    // makes var() fall back to each surface's own stock text colour, and it
    // must be set rather than left out: a share link on a stock accent inside
    // a branded instance would otherwise inherit the branding's text colour.
    '--accent-fg': accentForeground(color) ?? 'initial',
  }
}

/** Text colour for initials drawn on a user-chosen avatar colour: whichever of
 *  near-white and near-black reads better against it. */
export function readableTextOn(color: string): string {
  const rgb = parseHex(color)
  if (!rgb) return '#ffffff'
  return luminance(rgb) > 0.6 ? '#18181b' : '#ffffff'
}
