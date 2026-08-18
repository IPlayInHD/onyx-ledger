# BLOCKER — <one line: the provision that cannot be represented>

One report per blocked item. A blocker stops **that item only**; every
independent item in the batch continues.

This template exists to make a capability gap actionable without designing the
fix. It records what was demanded and what exists. Choosing between them is a
decision for an engineering entry, not for an ingestion run.

## Source

```
DRIVE_FILE_ID       =        <!-- the Drive object this was retrieved from -->
OFFICIAL_IDENTIFIER =
ISSUER              =
JURISDICTION        =
SOURCE_TYPE         =
EDITION             =
FINGERPRINT         =        <!-- full SHA-256; the version this rests on -->
FINGERPRINT_METHOD  =        <!-- raw bytes, or declared -->
SOURCE_VERSION_ID   =
QUALIFIES AS PRODUCTION AUTHORITY = YES / NO
```

## Locator

The exact structured location — section, subsection, paragraph, clause, page,
table, form line. Precise enough that a reviewer opens the document once and
lands on the provision.

```
LOCATOR =
```

"The guide" is not a locator. If you cannot name the location, the blocker is
that the provision was not located, and that is what to report.

## Provision

What the source actually says, quoted or closely paraphrased, with nothing
added. Keep interpretation out of this section — it belongs in the next one,
where it can be disagreed with.

## Required semantic

What the pipeline would have to be able to express for this provision to
publish correctly. Be concrete: name the operation, the shape, the cardinality,
the relationship.

State it as a capability, not as a preferred design.

## Existing capability

What the certified architecture provides today, **as discovered this run** —
not from memory, and not from this file. Cite the vocabulary or the operation
set you actually read in PHASE 0.

## Demonstrated mismatch

Show the gap rather than asserting it. The strongest form is an attempt that
failed with a machine code, or an example the current capability computes
differently from the source's own worked example.

```
ATTEMPTED     =
RESULT        =        <!-- validation codes, or the computed vs expected value -->
WHY IT FAILS  =
```

An assertion with no demonstration is a hypothesis. If the attempt was not
made, say that — an untested belief is worth recording, but it is worth
recording as untested.

## Smallest possible capability change

The **narrowest** change that would close this gap. Not a redesign, not a
family of related improvements — the smallest thing that makes this provision
representable.

```
CHANGE          =
BLAST RADIUS    =        <!-- what else it touches: schema, evaluator, sealed artifacts, replay -->
BACKWARD COMPAT =        <!-- does published knowledge keep its meaning and its hashes? -->
ALTERNATIVE     =        <!-- if the provision could be represented another way, say so -->
```

If a smaller change than the obvious one exists, prefer naming it. If the
honest answer is that a genuine redesign is required, say that plainly rather
than proposing a small change that would not actually work.

## Workarounds explicitly rejected

State what was *not* done and why, so nobody re-proposes it:

- approximating the provision
- widening a closed vocabulary to admit it
- inventing a formula operation
- encoding logic as free-form text
- publishing without a qualifying citation
- using an override

Each of these would have made the item publish. That is why they are listed.

## Scope of the block

```
BLOCKED ITEM    =
BATCH CONTINUED = YES / NO
ALSO AFFECTED   =        <!-- items sharing this gap; group them rather than repeating -->
PUBLISHED ANYWAY = must be NONE
```

Nothing depending on the missing semantic may publish in a degraded form. A
partially correct tax rule is worse than an absent one: absence is visible,
and a wrong answer is not.

## Variant — AUTHORITY_REVIEW_REQUIRED

Use this form when the blocker is not a missing capability but two
authoritative sources governing the same semantic over the same effective
scope, with evidence that does not settle the relationship. Fill both sides
symmetrically; an asymmetric report invites the reader to prefer the side you
described more fully.

```
SOURCE A =
DRIVE FILE ID =
SHA-256 =
LOCATOR =
PUBLICATION/EFFECTIVE INFO =

SOURCE B =
DRIVE FILE ID =
SHA-256 =
LOCATOR =
PUBLICATION/EFFECTIVE INFO =

MATERIAL DIFFERENCE =
EVIDENCE ALREADY INVESTIGATED =     <!-- internal titles, identifiers, in-source dates,
                                         stated effective periods, official locators -->
WHY AUTOMATIC RESOLUTION IS UNSAFE =
AFFECTED SEMANTIC WITHHELD = YES    <!-- must be YES -->
```

This variant needs **no capability change**. Do not propose one; the pipeline
can express the knowledge perfectly well once a human says which source
governs.

## Decision requested

What a human must decide, stated so it can be answered without re-reading the
batch:

- open an engineering entry for the capability change, or
- accept the provision as out of scope for now, or
- re-scope the batch around it

Do not open the entry from here.
