/* =========================================================================
   THE FRONTEND KEEPS NO LIST OF ITS OWN
   =========================================================================
   Onboarding and settings each carried a hard-coded array of provinces, and
   the shell carried a hard-coded array of tax years. They drifted from the
   engine immediately: the copy offered Quebec, which has brackets but no QPP
   or QPIP handling, so a Quebec customer would have been shown a confident
   figure computed with the wrong payroll contributions.

   The list now comes from `/config/launch-scope`, and the backend checks it
   against the engine's own resolved dataset before serving it. This test does
   not re-check the CONTENT — that is the backend's job and duplicating it here
   would recreate the very thing being removed. It checks that no copy has crept
   back in.
   ========================================================================= */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const FILES = [
  'src/pages/Onboarding.tsx',
  'src/pages/Settings.tsx',
  'src/components/Shell.tsx',
  'src/pages/Landing.tsx',
]

function source(file: string): string {
  return readFileSync(resolve(process.cwd(), file), 'utf8')
}

describe('no parallel jurisdiction registry', () => {
  for (const file of FILES) {
    it(`${file} declares no province list`, () => {
      const text = source(file)
      // A province array is recognisable by its two-letter codes appearing as
      // literal values, which is exactly how the old constants were written.
      const codes = text.match(/value: '(AB|BC|MB|NB|NL|NS|NT|NU|ON|PE|QC|SK|YT)'/g)
      expect(
        codes,
        `${file} hard-codes province codes; use the backend launch scope instead`,
      ).toBeNull()
    })

    it(`${file} declares no tax-year list`, () => {
      const text = source(file)
      // Two or more four-digit years in one array literal is a list; a single
      // starting value (the shell's DEFAULT_TAX_YEAR) is not.
      const arrays = text.match(/\[\s*20\d\d\s*,\s*20\d\d[\s\S]{0,40}?\]/g)
      expect(
        arrays,
        `${file} hard-codes a tax-year list; use the backend launch scope instead`,
      ).toBeNull()
    })
  }

  it('the forms actually ask the backend', () => {
    // Non-vacuity. Every assertion above is "this text is absent", which a file
    // that rendered no province field at all would satisfy perfectly.
    for (const file of ['src/pages/Onboarding.tsx', 'src/pages/Settings.tsx']) {
      expect(source(file), `${file} should read the launch scope`).toContain(
        'useLaunchScope',
      )
    }
    expect(source('src/components/Shell.tsx')).toContain('useLaunchScope')
  })
})
