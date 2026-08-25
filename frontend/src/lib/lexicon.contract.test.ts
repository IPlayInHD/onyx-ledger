/* =========================================================================
   THE EXHAUSTIVENESS GUARD
   =========================================================================
   TypeScript already stops a member of one of this file's unions from going
   unmapped. What it cannot see is the BACKEND adding a state — a new
   `ActionStatus`, a new reason code, a new lever — because the generated
   OpenAPI types declare all of these as plain `string`.

   So this test reads the engine's own source. For each governed vocabulary it
   parses the authoritative Python file, extracts the members, and requires the
   lexicon to cover exactly that set:

     a member in the backend with no lexicon entry   → FAIL (a customer would
                                                        have seen a raw code)
     a lexicon entry with no backend member          → FAIL (dead mapping, or
                                                        a rename nobody noticed)

   WHY PARSE RATHER THAN IMPORT. The frontend cannot import Python, and a
   hand-copied list of members in this file would be the same enumeration
   failure the repository has already been bitten by twice — a list that was
   correct when written and silently stopped being the truth. Reading the file
   the engine actually uses is the only version that cannot drift.

   IF THE BACKEND MOVES. These tests fail loudly with the path they could not
   read, rather than passing over a file that no longer exists — a guard that
   silently matches nothing is worse than no guard.
   ========================================================================= */
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe as suite, expect, it } from 'vitest'
import {
  ACTION_STATUS,
  ASSURANCE_FAMILY,
  ASSURANCE_STATUS,
  EVIDENCE_READINESS,
  OPPORTUNITY_CODE,
  REASON_CODE,
  RESOURCE_CODE,
  URGENCY_STATUS,
} from './lexicon'

const HERE = dirname(fileURLToPath(import.meta.url))
const BACKEND = resolve(HERE, '../../../backend')

function read(relative: string): string {
  const path = resolve(BACKEND, relative)
  try {
    return readFileSync(path, 'utf8')
  } catch {
    throw new Error(
      `the lexicon guard cannot read ${relative}. If the engine moved, point ` +
        `this test at its new home — do not delete the check, and do not ` +
        `replace it with a hand-written list of members.`,
    )
  }
}

/** Values of a Python `StrEnum` — the string a payload actually carries. */
function enumValues(source: string, className: string): string[] {
  const start = source.indexOf(`class ${className}(StrEnum):`)
  if (start < 0) throw new Error(`class ${className}(StrEnum) not found`)
  const rest = source.slice(start)
  const end = rest.slice(1).search(/\n(?:class |def |@)/)
  const body = end < 0 ? rest : rest.slice(0, end + 1)
  const out: string[] = []
  for (const line of body.split('\n')) {
    const named = /^\s{4}([A-Z][A-Z0-9_]*)\s*=\s*"([^"]+)"/.exec(line)
    if (named) {
      out.push(named[2]!)
      continue
    }
    const auto = /^\s{4}([A-Z][A-Z0-9_]*)\s*=\s*auto\(\)/.exec(line)
    // `StrEnum` + `auto()` yields the LOWER-CASED member name.
    if (auto) out.push(auto[1]!.toLowerCase())
  }
  if (out.length === 0) throw new Error(`no values parsed from ${className}`)
  return out
}

function expectExactCoverage(backendMembers: string[], table: Record<string, unknown>, what: string) {
  const mapped = new Set(Object.keys(table))
  const missing = backendMembers.filter((m) => !mapped.has(m))
  const extra = [...mapped].filter((m) => !backendMembers.includes(m))

  expect(
    missing,
    `${what}: the engine can emit these and the lexicon has no words for them, so a ` +
      `customer would see the raw code. Add an entry to src/lib/lexicon.ts.`,
  ).toEqual([])
  expect(
    extra,
    `${what}: the lexicon maps these and the engine no longer emits them — either a ` +
      `rename went unnoticed or the entry is dead.`,
  ).toEqual([])
}

suite('the lexicon covers every governed state the engine can emit', () => {
  const assuranceSource = read('app/services/state_graph/assurance.py')
  /* `EvidenceReadiness` and `NodeType` are DECLARED in contracts.py and
     re-exported through assurance.py. The guard reads them where they are
     defined — the first version of this test looked only in assurance.py and
     failed loudly rather than silently matching nothing, which is the
     behaviour intended when a vocabulary moves. */
  const contractsSource = read('app/services/state_graph/contracts.py')
  const leversSource = read('app/services/ioe/domain/levers.py')

  it('covers every AssuranceStatus', () => {
    expectExactCoverage(enumValues(assuranceSource, 'AssuranceStatus'), ASSURANCE_STATUS, 'AssuranceStatus')
  })

  it('covers every ActionStatus', () => {
    expectExactCoverage(enumValues(assuranceSource, 'ActionStatus'), ACTION_STATUS, 'ActionStatus')
  })

  it('covers every UrgencyStatus', () => {
    expectExactCoverage(enumValues(assuranceSource, 'UrgencyStatus'), URGENCY_STATUS, 'UrgencyStatus')
  })

  it('covers every EvidenceReadiness', () => {
    expectExactCoverage(
      enumValues(contractsSource, 'EvidenceReadiness'),
      EVIDENCE_READINESS,
      'EvidenceReadiness',
    )
  })

  it('covers every assurance family (NodeType)', () => {
    expectExactCoverage(enumValues(contractsSource, 'NodeType'), ASSURANCE_FAMILY, 'NodeType')
  })

  it('covers every reason code the assurance module can attach', () => {
    /* Reason codes are module-level string constants rather than an enum. The
       engine names them `REVIEW_*`, `*_AUTHORITATIVE`, `NO_*_RUN_*` and so on;
       what they have in common is that they are SCREAMING_SNAKE string literals
       assigned at module level in this one file. Anything that is already an
       enum value is excluded — those are covered above. */
    const enumValueSet = new Set([
      ...enumValues(assuranceSource, 'AssuranceStatus'),
      ...enumValues(assuranceSource, 'ActionStatus'),
      ...enumValues(assuranceSource, 'UrgencyStatus'),
      ...enumValues(contractsSource, 'EvidenceReadiness'),
      ...enumValues(contractsSource, 'NodeType'),
      ...enumValues(contractsSource, 'EdgeType'),
    ])
    /* EVERY screaming-snake string literal in the file, not only the ones
       assigned to a module-level constant. `PORTFOLIO_EXCLUSION` is written
       inline at the point it is attached, and the first version of this guard
       missed it — which would have let exactly the class of code this entry
       exists to remove reach a customer. */
    const declared = [...assuranceSource.matchAll(/"([A-Z][A-Z0-9_]{3,})"/g)].map((m) => m[1]!)
    const reasonCodes = [...new Set(declared)].filter((c) => !enumValueSet.has(c))

    expect(reasonCodes.length, 'no reason codes parsed — the guard is watching nothing').toBeGreaterThan(0)
    expectExactCoverage(reasonCodes, REASON_CODE, 'reason codes')
  })

  it('covers every registered lever', () => {
    /* `\bcode=` and not `code=`: `shared_resource_code="RRSP_ROOM"` ends in the
       same four characters and is a DIFFERENT vocabulary — a pool of room a
       lever draws on, not a move the customer can make. Conflating the two is
       the mistake this guard caught on its first run. */
    const codes = [...leversSource.matchAll(/(?:^|[\s(])code="([A-Z][A-Z0-9_]*)"/gm)].map(
      (m) => m[1]!,
    )
    const unique = [...new Set(codes)]
    expect(unique.length, 'no levers parsed — the guard is watching nothing').toBeGreaterThan(0)
    expectExactCoverage(unique, OPPORTUNITY_CODE, 'lever registry')
  })

  it('covers every shared resource a lever draws on', () => {
    const codes = [
      ...leversSource.matchAll(/shared_resource_code="([A-Z][A-Z0-9_]*)"/g),
    ].map((m) => m[1]!)
    const unique = [...new Set(codes)]
    expect(unique.length, 'no resources parsed — the guard is watching nothing').toBeGreaterThan(0)
    expectExactCoverage(unique, RESOURCE_CODE, 'shared resource codes')
  })
})
