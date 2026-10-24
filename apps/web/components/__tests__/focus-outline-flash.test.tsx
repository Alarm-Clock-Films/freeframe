/**
 * Focusing a text field flashed a white ring that faded away.
 *
 * Tailwind's outline-none is `outline: 2px solid transparent`, not "no outline".
 * As `focus:outline-none` (or `focus-visible:`) it takes effect only on focus, so
 * on an element with transition-all the outline animates from its unfocused
 * colour, the text colour (near-white in the dark theme), to transparent: a solid
 * ring that flashes and fades. Plain `outline-none` holds before and after focus,
 * so there is nothing to animate.
 */
import { describe, expect, it } from 'vitest'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import { render, screen } from '@testing-library/react'
import { Input } from '../ui/input'
import { Button } from '../ui/button'

const WEB_ROOT = join(__dirname, '..', '..')
const STATE_OUTLINE_NONE = /(?:^|[\s'"`])(?:[\w-]+:)*(?:focus|focus-visible|focus-within):outline-none(?=[\s'"`]|$)/
const TRANSITION_ALL = /(?:^|[\s'"`])(?:[\w-]+:)*transition-all(?=[\s'"`]|$)/

function sourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name)
    if (['node_modules', '.next', '__tests__'].includes(name)) return []
    if (statSync(path).isDirectory()) return sourceFiles(path)
    return /\.(tsx|ts)$/.test(name) ? [path] : []
  })
}

describe('focus outline flash', () => {
  it('Input keeps its outline transparent before focus too', () => {
    render(<Input aria-label="Email" />)
    const classes = screen.getByLabelText('Email').className.split(/\s+/)
    expect(classes).toContain('outline-none')
    expect(classes).not.toContain('focus:outline-none')
  })

  it('Button keeps its outline transparent before focus too', () => {
    render(<Button>Save</Button>)
    const classes = screen.getByRole('button', { name: 'Save' }).className.split(/\s+/)
    expect(classes).toContain('outline-none')
    expect(classes).not.toContain('focus-visible:outline-none')
  })

  it('no component pairs transition-all with a focus-only outline-none', () => {
    const offenders: string[] = []
    for (const file of ['app', 'components'].flatMap((d) => sourceFiles(join(WEB_ROOT, d)))) {
      const lines = readFileSync(file, 'utf8').split('\n')
      lines.forEach((line, i) => {
        // Comments may name the classes; only code counts.
        if (/^\s*(\/\/|\*|\/\*)/.test(line)) return
        if (!STATE_OUTLINE_NONE.test(line)) return
        // A class list can span the lines of a cn(...) call.
        const nearby = lines.slice(Math.max(0, i - 4), i + 5).join(' ')
        if (TRANSITION_ALL.test(nearby)) offenders.push(`${relative(WEB_ROOT, file)}:${i + 1}`)
      })
    }
    expect(offenders).toEqual([])
  })
})
