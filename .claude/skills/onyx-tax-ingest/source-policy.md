# Source policy — identity, dedup, classification, authority

## 1. Identity is content, never a filename

Fingerprint every candidate file with SHA-256 **over its exact bytes**, before
anything else. The registry's own helper is the one to use, so the digest a
batch computes is the digest the registry stores:

```python
from app.services.tax_kb.sources.domain import fingerprint_bytes
digest = fingerprint_bytes(raw)          # sha256 hex over the exact bytes
```

Hash the bytes as retrieved. Do **not** canonicalize, normalize line endings,
re-save, or run a PDF through a rewriter first — canonicalization makes
*structured values* comparable, and a document's identity is its literal bytes.
A digest of your reading of a document is a different claim from a digest of
the document.

Then apply exactly two rules:

| Observation | Meaning | Action |
|---|---|---|
| Same bytes as a registered version | the same document | **DUPLICATE — skip** |
| Different bytes | a different edition | **new source-version candidate** |

**Filename equality is not identity, and filename difference is not
difference.** Both halves matter, and real libraries break both:

- `brackets-2025.pdf` and `brackets-2025.pdf.pdf` at identical byte length are
  very likely one document stored twice — the fingerprint decides, not the name.
- `rrsp-tfsa-limits.pdf` and `rrsp-tfsa-limits .pdf` differing only by a space
  in the name but differing in size are **two different documents**, and
  treating them as a naming accident would silently drop one.

Expect trailing spaces before extensions, doubled extensions, non-ASCII dashes,
and truncated year prefixes. Report names **verbatim**; never rename to tidy
them. Key everything on fingerprint plus registry id, never on a path string.

**Do not re-read unchanged sources.** A file whose digest is already registered
needs no download, no extraction and no re-validation. That is the whole point
of keeping the index.

## 2. Timestamps are not change detection

Uploaded libraries routinely carry `modifiedTime` *earlier* than `createdTime`,
because a copy preserved the source mtime. Any freshness logic built on
timestamps will be wrong in both directions. **The fingerprint is the only
change signal.**

## 3. Classification, and what you may not invent

Each new file needs: issuer, jurisdiction, source type, effective scope, and an
official identifier where one exists.

Read the current vocabularies at run time (PHASE 0). Then:

- **Issuer** is a controlled code, so "CRA" and "Canada Revenue Agency" cannot
  become two publishers of one guide. If the issuer is genuinely not in the
  vocabulary, that is a blocker, not a reason to pick the nearest one.
- **Jurisdiction** reuses `ref.jurisdiction`. Never create a second
  jurisdiction vocabulary. A provincial source must not register as federal.
- **Source type** says what *kind* of authority a document is. It does **not**
  rank authority — nothing in this repository governs legal precedence, and
  choosing a type to make something publishable is falsifying provenance.
- **Tax year is optional.** Statutes are date-based. Forcing a tax year onto
  the Income Tax Act invents a fact; leave it absent and use the effective
  period.
- **Official identifier** is the publisher's own (`T4002`, `RC4022`, an ITA
  citation). If a document has none, say so rather than coining one.

Every field is a claim about the document. If the document does not support the
claim, the field is empty and the item is escalated.

## 4. Registration is append-only

```python
from app.services.tax_kb.sources.domain import manifest_from_payload, SourceLocator
from app.services.tax_kb.sources.registry import TaxSourceRegistry

version = await TaxSourceRegistry(session).register(manifest_from_payload(payload))
```

Properties to rely on and not work around:

- **Re-registering identical bytes is idempotent** — it returns the existing
  version rather than creating a second one. Safe to retry.
- **A different fingerprint never overwrites.** It becomes a new version.
- **The manifest rejects unknown fields.** A field the registry drops is a
  claim its author made that nothing recorded.
- **Registration writes metadata, not content.** Raw documents are never stored
  in the database. Hash, cite, discard.

### Fingerprint method is part of the claim

`RAW_BYTES_SHA256` means you hashed the actual bytes. `DECLARED_BY_OPERATOR`
means a digest arrived without them. Use the strong method whenever bytes are
retrievable, and never label a declared digest as a raw one — a registry
storing only the hex string would let the weaker claim read as the stronger.

### Supersession is declared, never inferred

A newer edition does not automatically supersede an older one. Supersession is
recorded by the **successor naming its predecessor's fingerprint**, so no
historical row is ever rewritten. Never infer it from dates, titles, or the
order files appear in a folder.

When a library contains several editions of the same document with different
bytes, **which one is authoritative is an operator decision.** Escalate it; do
not pick the largest, the newest, or the one with the tidiest filename.

## 5. Citations are structured, and reusable

A citation is a **source version plus a structured locator** — section,
subsection, paragraph, page, table, form line and so on, from the closed key
set discovered in PHASE 0. Not a page number scribbled into prose, and not a
free-form string.

```python
citation = await registry.cite(version.id, SourceLocator({"section": "118.2",
                                                          "subsection": "2"}))
await registry.attach(citation.id, rule_version_id=draft_version_id)
```

Locator identity ignores key order, so the same location cited by twenty rules
is **one row** rather than twenty copies of the source metadata. Cite precisely:
"the guide" is not a locator, and a citation that points at a whole document
tells a later reviewer nothing about which words the rule rests on.

Resolve provenance for a whole batch in **one** call rather than per object.

## 6. Qualifying production authority

Registered and *qualifying* are different questions.

The registry deliberately declines to rank sources. The **publication policy**
answers a narrower product question that policy legitimately can: may this
*kind* of source stand alone behind a figure a user acts on? Read the current
allow list in PHASE 0.

- Commentary about the law may be registered, cited, and read **alongside** a
  qualifying source. It may not be the only thing behind a published figure.
- A **withdrawn** edition never carried the authority it appears to. It blocks.
- A **superseded** edition is not a broken one. A rule authored in 2024 rests
  on the words in force then; the pinned edition is used unchanged and the
  supersession is reported for review, never silently swapped for the newer
  text.
- Provenance that cannot be resolved is an **integrity failure**, not a stale
  citation. The registry raises rather than substituting today's edition.

## 7. What must never happen

- A production knowledge object with no governed citation.
- A free-form URL or a quoted paragraph standing in for provenance.
- A manifest restating an issuer, title, URL or section text — the registry is
  canonical, and a copy is a second record that will diverge.
- Fabricated provenance for test or historical data.
- A source file modified, renamed, moved or deleted.
