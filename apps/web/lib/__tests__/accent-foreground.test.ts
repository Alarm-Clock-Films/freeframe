/**
 * Text colour on accent buttons and badges (lib/accent.ts).
 *
 * A custom accent gets whichever of white and near-black has the higher WCAG
 * contrast against it. The stock accents keep the text colour each surface was
 * designed with, which accentVars expresses as `--accent-fg: initial`.
 */
import { describe, expect, it } from 'vitest'
import {
  ACCENT_TEXT_DARK,
  ACCENT_TEXT_LIGHT,
  ACCENT_VAR_NAMES,
  STOCK_ACCENTS,
  accentForeground,
  accentVars,
  contrastRatio,
  readableTextOn,
} from '../accent'

const LOGO_YELLOW = '#FBFF00'

/** `rgb(r g b)`, as accentVars writes the hover shade, to hex. */
function rgbToHex(rgb: string): string {
  const [r, g, b] = rgb.match(/\d+/g)!.map(Number)
  return '#' + [r, g, b].map((c) => c.toString(16).padStart(2, '0')).join('')
}

describe('contrastRatio', () => {
  it('matches the WCAG reference values', () => {
    expect(contrastRatio('#000000', '#ffffff')).toBeCloseTo(21, 5)
    expect(contrastRatio('#ffffff', '#ffffff')).toBe(1)
    // WCAG's own worked example: #777777 on white is 4.48:1.
    expect(contrastRatio('#777777', '#ffffff')).toBeCloseTo(4.48, 2)
  })

  it('is symmetric and case-insensitive', () => {
    expect(contrastRatio('#FBFF00', '#18181b')).toBeCloseTo(contrastRatio('#18181B', '#fbff00'), 10)
  })
})

describe('accentForeground', () => {
  it('puts dark text on the logo yellow, at over 16:1', () => {
    expect(accentForeground(LOGO_YELLOW)).toBe(ACCENT_TEXT_DARK)
    expect(contrastRatio(LOGO_YELLOW, ACCENT_TEXT_DARK)).toBeGreaterThan(16)
    // What it replaces: white on yellow.
    expect(contrastRatio(LOGO_YELLOW, ACCENT_TEXT_LIGHT)).toBeLessThan(1.2)
  })

  it.each([
    ['#ed7d21', 'orange'],
    ['#22c55e', 'green'],
    ['#14b8a6', 'teal'],
    ['#f59e0b', 'amber'],
  ])('puts dark text on mid tones where white fails (%s, %s)', (color) => {
    expect(accentForeground(color)).toBe(ACCENT_TEXT_DARK)
    expect(contrastRatio(color, ACCENT_TEXT_LIGHT)).toBeLessThan(3)
    expect(contrastRatio(color, ACCENT_TEXT_DARK)).toBeGreaterThan(6)
  })

  it.each(['#1e3a8a', '#7c2d12', '#000000', '#5b21b6'])('keeps white on dark accents (%s)', (color) => {
    expect(accentForeground(color)).toBe(ACCENT_TEXT_LIGHT)
  })

  it('leaves the stock blues alone, however they are written', () => {
    for (const stock of STOCK_ACCENTS) {
      expect(accentForeground(stock)).toBeNull()
      expect(accentForeground(stock.toUpperCase())).toBeNull()
      expect(accentForeground(` ${stock} `)).toBeNull()
    }
  })

  it('ignores anything that is not a 6-digit hex', () => {
    for (const bad of ['', 'yellow', '#fff', '#fbff0', 'rgb(1,2,3)']) {
      expect(accentForeground(bad)).toBeNull()
    }
  })

  it('always picks the better of the two, and never below 4.2:1', () => {
    // 4.2:1 is the most any colour can get from one of these two text
    // colours at the point where white and near-black score the same.
    for (let r = 0; r < 256; r += 17)
      for (let g = 0; g < 256; g += 17)
        for (let b = 0; b < 256; b += 17) {
          const color = '#' + [r, g, b].map((c) => c.toString(16).padStart(2, '0')).join('')
          const fg = accentForeground(color)!
          const other = fg === ACCENT_TEXT_DARK ? ACCENT_TEXT_LIGHT : ACCENT_TEXT_DARK
          expect(contrastRatio(color, fg)).toBeGreaterThanOrEqual(contrastRatio(color, other))
          expect(contrastRatio(color, fg)).toBeGreaterThan(4.2)
        }
  })
})

describe('accentVars', () => {
  it('sets --accent-fg from a custom accent', () => {
    expect(accentVars(LOGO_YELLOW)!['--accent-fg']).toBe(ACCENT_TEXT_DARK)
    expect(accentVars('#1e3a8a')!['--accent-fg']).toBe(ACCENT_TEXT_LIGHT)
  })

  it('resets it for a stock accent, so surfaces keep their stock text colour', () => {
    // Set, not omitted: a share link on the stock blue inside an instance
    // branded yellow must not inherit the yellow's dark text.
    for (const stock of STOCK_ACCENTS) {
      expect(accentVars(stock)!['--accent-fg']).toBe('initial')
    }
  })

  it('keeps the text readable on the hover shade too', () => {
    const vars = accentVars(LOGO_YELLOW)!
    expect(contrastRatio(rgbToHex(vars['--accent-hover']), vars['--accent-fg'])).toBeGreaterThan(7)
  })

  it('lists --accent-fg among the variables branding clears', () => {
    expect(ACCENT_VAR_NAMES).toContain('--accent-fg')
  })
})

describe('readableTextOn (avatars)', () => {
  it('is unchanged by this', () => {
    expect(readableTextOn(LOGO_YELLOW)).toBe('#18181b')
    expect(readableTextOn('#ed7d21')).toBe('#ffffff')
  })
})

describe('the text-accent-fg* utilities', () => {
  it("fall back to each surface's stock text colour when no custom accent is set", async () => {
    const { default: config } = await import('../../tailwind.config')
    const accent = (config.theme!.extend!.colors as Record<string, Record<string, string>>).accent
    expect(accent.fg).toBe('var(--accent-fg, #ffffff)')
    expect(accent['fg-inverse']).toBe('var(--accent-fg, var(--text-inverse))')
    expect(accent['fg-primary']).toBe('var(--accent-fg, var(--text-primary))')
  })
})
