/* =========================================================================
   ONYX ONLY OFFERS WHAT IT CAN ANSWER
   =========================================================================
   The onboarding and settings forms once offered Quebec, on the reasoning that
   the engine carries Quebec brackets. It does — and that was not enough. A
   Quebec resident pays QPP rather than CPP and pays QPIP premiums, and the tax
   engine implements neither, so the figure a Quebec customer would have been
   shown was computed with the wrong payroll contributions and presented with
   the same confidence as a correct one.

   Offering a jurisdiction is a claim that Onyx can answer for it. This test
   keeps the two province lists identical and keeps Quebec out of both until
   the engine can actually answer for it.
   ========================================================================= */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

function offeredProvinces(file: string): string[] {
  const source = readFileSync(resolve(process.cwd(), file), 'utf8')
  const block = /const PROVINCES: readonly Option\[\] = \[(.*?)\]/s.exec(source)
  expect(block?.[1], `${file} must declare a PROVINCES list`).toBeTruthy()
  return [...block![1]!.matchAll(/value: '([A-Z]{2})'/g)].map((m) => m[1]!)
}

const FORMS = ['src/pages/Onboarding.tsx', 'src/pages/Settings.tsx']

describe('offered jurisdictions', () => {
  for (const form of FORMS) {
    it(`${form} does not offer a province the engine cannot answer for`, () => {
      const offered = offeredProvinces(form)
      expect(offered.length).toBeGreaterThan(0)
      // Quebec: no QPP, no QPIP, no governed published brackets, and marked a
      // deferred jurisdiction by the content-ingestion policy.
      expect(offered, 'Quebec is not answerable yet').not.toContain('QC')
      // The territories and the remaining provinces have no bracket data at
      // all; offering one would be accepting input Onyx cannot price.
      for (const absent of ['NU', 'NT', 'YT', 'NL', 'PE', 'NS', 'NB', 'MB', 'SK']) {
        expect(offered, `${absent} has no bracket data`).not.toContain(absent)
      }
    })
  }

  it('offers exactly the same set everywhere it asks', () => {
    // Two lists that drift let a customer pick a province during onboarding
    // that they can never change back to in settings, or the reverse.
    const [onboarding, settings] = FORMS.map(offeredProvinces)
    expect([...settings!].sort()).toEqual([...onboarding!].sort())
  })
})
