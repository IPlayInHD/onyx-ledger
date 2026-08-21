/* =========================================================================
   CONTRAST IS A TOKEN INVARIANT, NOT A SCREEN INVARIANT
   =========================================================================
   The browser accessibility scan catches contrast only on surfaces it can
   reach, in the one theme the test browser happens to run. That left a real
   hole: a text token failing AA at 3.15:1 in DARK mode passed every scan we
   had, because Chromium ran light.

   So the ladder is asserted here, against the stylesheet itself. Each text
   token is checked at its WORST case — the darkest light surface it can land
   on, the lightest dark surface — which is the only bound that holds no
   matter which panel a component is dropped into.

   These tokens are used at 11–12px, so WCAG AA applies at the full 4.5:1;
   the 3:1 large-text allowance is not available to them.
   ========================================================================= */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

// Read the shipped stylesheet, not a copy of the values: a test that restated
// the palette would keep passing after someone edited the real one.
const css = readFileSync(resolve(process.cwd(), 'src/styles/tokens.css'), 'utf8')

/** WCAG 2.x relative luminance. */
function luminance(hex: string): number {
  const value = hex.replace('#', '')
  const channels = [0, 2, 4].map((offset) => {
    const raw = Number.parseInt(value.slice(offset, offset + 2), 16) / 255
    return raw <= 0.04045 ? raw / 12.92 : ((raw + 0.055) / 1.055) ** 2.4
  }) as [number, number, number]
  return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]
}

function contrast(foreground: string, background: string): number {
  const a = luminance(foreground)
  const b = luminance(background)
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)
}

/**
 * Read a token from a specific palette block.
 *
 * The dark palette is declared twice on purpose — once under the system
 * preference and once under an explicit `[data-theme='dark']` choice — so the
 * block is selected by index and BOTH copies are asserted. A fix applied to
 * only one of them would leave the theme toggle disagreeing with the OS.
 */
function paletteBlocks(): { name: string; body: string; dark: boolean }[] {
  const media = css.indexOf('@media (prefers-color-scheme: dark)')
  const explicit = css.indexOf(":root[data-theme='dark']")
  expect(media, 'dark media block present').toBeGreaterThan(0)
  expect(explicit, 'explicit dark theme block present').toBeGreaterThan(0)
  return [
    { name: 'light :root', body: css.slice(0, media), dark: false },
    { name: 'dark (system preference)', body: css.slice(media, explicit), dark: true },
    { name: 'dark (explicit choice)', body: css.slice(explicit), dark: true },
  ]
}

function token(body: string, name: string): string {
  const match = new RegExp(`${name}:\\s*(#[0-9a-fA-F]{6})`).exec(body)
  if (!match?.[1]) throw new Error(`token ${name} not found in palette block`)
  return match[1].toLowerCase()
}

/** Every ground a token can legitimately sit on. The worst pairing governs. */
const SURFACES = ['--c-paper', '--c-surface', '--c-surface-sunken', '--c-surface-inset']

/** Text tokens and the floor each must clear on every surface. */
const TEXT_TOKENS = [
  '--c-ink',
  '--c-ink-secondary',
  '--c-ink-muted',
  '--c-ink-faint',
  '--c-accent',
  '--c-positive',
  '--c-negative',
  '--c-caution',
  '--prov-governed',
  '--prov-user',
  '--prov-assumption',
  '--prov-ai',
]

const AA_TEXT = 4.5
/** 1.4.11: a meaningful graphical object (an axis, a rule that carries data). */
const AA_NON_TEXT = 3

describe('design tokens meet WCAG AA in every theme', () => {
  for (const block of paletteBlocks()) {
    describe(block.name, () => {
      for (const name of TEXT_TOKENS) {
        it(`${name} clears ${AA_TEXT}:1 on the worst surface`, () => {
          const foreground = token(block.body, name)
          const worst = SURFACES.map((surface) => ({
            surface,
            ratio: contrast(foreground, token(block.body, surface)),
          })).reduce((a, b) => (a.ratio <= b.ratio ? a : b))

          expect(
            Number(worst.ratio.toFixed(2)),
            `${name} (${foreground}) on ${worst.surface} — small text needs ${AA_TEXT}:1`,
          ).toBeGreaterThanOrEqual(AA_TEXT)
        })
      }

      it('--viz-axis clears the 3:1 graphical threshold', () => {
        const axis = token(block.body, '--viz-axis')
        const worst = Math.min(
          ...SURFACES.map((surface) => contrast(axis, token(block.body, surface))),
        )
        expect(Number(worst.toFixed(2)), `--viz-axis (${axis})`).toBeGreaterThanOrEqual(
          AA_NON_TEXT,
        )
      })

      it('the ink ladder stays monotonic, so hierarchy still reads', () => {
        // Raising the quiet tiers to pass AA compresses them. They must still
        // descend in order, or "muted" and "faint" become the same tier wearing
        // two names and the visual hierarchy is a lie.
        const ground = token(block.body, '--c-surface-inset')
        const ladder = ['--c-ink', '--c-ink-secondary', '--c-ink-muted', '--c-ink-faint'].map(
          (name) => contrast(token(block.body, name), ground),
        )
        for (let i = 1; i < ladder.length; i += 1) {
          expect(ladder[i]!, `${i}: each tier must be quieter than the one above`).toBeLessThan(
            ladder[i - 1]!,
          )
        }
      })
    })
  }

  it('both dark declarations agree, so the toggle matches the system', () => {
    const [, system, explicit] = paletteBlocks()
    for (const name of [...TEXT_TOKENS, ...SURFACES, '--viz-axis']) {
      expect(token(explicit!.body, name), `${name} differs between dark blocks`).toBe(
        token(system!.body, name),
      )
    }
  })
})
