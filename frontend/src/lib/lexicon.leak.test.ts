/* =========================================================================
   THE PRETTIFIER MUST NOT BE A FALLBACK FOR A GOVERNED STATE
   =========================================================================
   The lexicon returns `null` for a state nobody has written words for, and
   that null has to mean "say nothing". The failure mode this guards is one
   line long and reads as helpful:

       level1(vocab, code) ?? technical(vocab, code) ?? humanize(code)

   With that in place a code the lexicon has never heard of becomes
   confident-looking prose again, the exhaustiveness guard still passes — it
   only checks the MAPPING — and nothing on screen says the sentence was
   generated rather than written. Reintroducing exactly that line was measured
   against the whole frontend suite and 64 tests passed, which is why this file
   exists.

   Source-level, in the same spirit as `styles/no-inline-styles.test.ts`: the
   rule is about how the code is written, so it is checked where the code is.
   ========================================================================= */
import { readFileSync, readdirSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe as suite, expect, it } from 'vitest'

const HERE = dirname(fileURLToPath(import.meta.url))
const SRC = resolve(HERE, '..')

function sourceFiles(dir: string): string[] {
  const out: string[] = []
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name)
    if (entry.isDirectory()) out.push(...sourceFiles(full))
    else if (/\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) out.push(full)
  }
  return out
}

/**
 * Code only, on one line.
 *
 * COMMENTS ARE STRIPPED FIRST, and that is not a convenience. This guard's own
 * first run flagged `Opportunities.tsx` for a comment that QUOTES the defect it
 * replaced — "Was `humanize(item.opportunity_code)`, which rendered…". A rule
 * that forbids describing the mistake it prevents is a rule that deletes its own
 * documentation. The same lesson as the lexicon contract test, which parses the
 * backend rather than grepping it.
 *
 * Flattened afterwards so a `??` chain split across lines is still one
 * expression to the pattern.
 */
function flattened(source: string): string {
  return source
    .replace(/\/\*[\s\S]*?\*\//g, ' ')
    .replace(/(^|[^:])\/\/[^\n]*/g, '$1 ')
    .replace(/\s+/g, ' ')
}

suite('a lexicon lookup never falls back to a prettifier', () => {
  const files = sourceFiles(SRC)

  it('finds source to check', () => {
    expect(files.length, 'no source files found — this guard is watching nothing').toBeGreaterThan(5)
  })

  it('has no `level1(...) ?? ... humanize(...)` chain anywhere', () => {
    /* A `??` chain that starts at the lexicon and ends at a prettifier. The
       lexicon's null is the signal to say nothing; humanize turns it into a
       sentence. */
    const CHAIN = /\b(?:level1|level2|technical)\s*\([^)]*\)(?:\s*\?\?\s*(?:level1|level2|technical)\s*\([^)]*\))*\s*\?\?\s*humanize(?:Code)?\s*\(/
    const offenders: string[] = []
    for (const file of files) {
      const text = flattened(readFileSync(file, 'utf8'))
      if (CHAIN.test(text)) offenders.push(file.replace(`${SRC}/`, ''))
    }
    expect(
      offenders,
      'these fall back to a prettifier when the lexicon has no words for a governed ' +
        'state, which turns an unreviewed code into prose that reads reviewed. Render ' +
        'nothing, or an explicitly approved controlled string — never the code itself.',
    ).toEqual([])
  })

  /* SURFACES THIS ENTRY HAS NOT REACHED YET.
     Named one by one, never a wildcard, and the test below FAILS IF ONE OF
     THEM IS FIXED AND LEFT ON THE LIST — so the exemption cannot rot into a
     permanent hole the way a blanket allow-list would. Shrinking this to
     nothing is the remaining work; growing it requires deliberately editing
     this file, which is the point. */
  const DEFERRED = new Set(['pages/BeforeYouAct.tsx', 'pages/Evidence.tsx'])

  it('has no `humanize(...)` on a value named like a governed status', () => {
    /* The other direction: prettifying a field that IS one of the vocabularies
       the lexicon owns, without consulting it at all. */
    const GOVERNED_FIELD =
      /\bhumanize(?:Code)?\s*\(\s*[A-Za-z_$][\w$.]*\.(?:status|action|urgency|opportunity_code|reason_code|evidence_readiness|family|eligibility_status)\b/
    const offenders: string[] = []
    for (const file of files) {
      const text = flattened(readFileSync(file, 'utf8'))
      if (GOVERNED_FIELD.test(text)) offenders.push(file.replace(`${SRC}/`, ''))
    }

    const unexpected = offenders.filter((file) => !DEFERRED.has(file))
    expect(
      unexpected,
      'these prettify a field the lexicon owns. Use level1()/technical() so the ' +
        'wording is one reviewed string rather than a reformatted identifier.',
    ).toEqual([])

    /* The list cleans itself. A surface that has been converted must come off
       DEFERRED, or the exemption outlives the problem and quietly re-permits
       the defect the day someone reintroduces it. */
    const stale = [...DEFERRED].filter((file) => !offenders.includes(file))
    expect(
      stale,
      'these are on the deferred list and no longer offend. Remove them from ' +
        'DEFERRED so the exemption does not outlive the problem.',
    ).toEqual([])
  })
})
