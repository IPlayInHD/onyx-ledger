# Source policy — identity, dedup, classification, authority

## 0. Three different identities. Do not conflate them.

| Concept | What it is | Used for |
|---|---|---|
| **Drive object identity** | the **Drive file ID** | addressing, retrieval, operational bookkeeping |
| **Content identity** | **`RAW_BYTES_SHA256`** | duplicate detection, change detection |
| **Governed semantic identity** | the certified **Source Registry** contract (source + version) | provenance, citation, publication |

A filename is **display and discovery metadata only** — never identity. Drive
timestamps are **auxiliary metadata only** — never identity.

Two Drive file IDs may carry identical bytes (one content identity, two Drive
objects). One Drive object's bytes may change (one Drive identity, two content
identities). Neither situation is confusing once the three are kept apart, and
every mistake in this area comes from collapsing them.

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
historical row is ever rewritten.

**Never establish supersession from any of these:**

filename · file size · Drive upload date · Drive modified date · alphabetical
order · presumed recency · which file looks cleaner · which file seems more
likely to be newer

Every one of those is a guess wearing the costume of a fact. Supersession is
established **only** when evidence from the authoritative source supports it —
an explicit replacement notice, a stated edition sequence, a publication date
printed *inside* the document, or an official identifier that encodes the
succession.

## 4b. Contested source families — investigate before escalating

When several files look like editions of one document, **do not ask an operator
to choose based on filenames.** Filenames are the least reliable evidence
available, and the question is answerable from the documents themselves.

Work the evidence in this order:

1. retrieve exact raw bytes for each candidate
2. compute `RAW_BYTES_SHA256`
3. compare the digests
4. inspect the **internal document title**
5. inspect the **official document identifier** where present
6. inspect the **publication or revision date inside the source**
7. determine the **stated tax year or effective period**
8. inspect the **official source URL/locator** printed in the document
9. where digests differ, compare the **substantive content** that matters for
   the semantic being ingested

Only then classify.

### CASE A — identical SHA-256

```
EXACT_DUPLICATE
```

One content identity behind several Drive objects. **Process the content once.**
Retain the fact that multiple Drive file IDs point at it if that is useful for
bookkeeping — but never generate duplicate semantic knowledge, and never
register it twice (registration is idempotent on the fingerprint anyway).

### CASE B — different SHA-256

```
DISTINCT_SOURCE_SNAPSHOT
```

Different bytes mean genuinely different documents. **Do not automatically call
either one a successor.** Investigate metadata and content per the list above,
then either establish supersession on evidence or leave them as independent
snapshots.

### Unresolved authority conflict

If two genuinely distinct authoritative versions appear to govern **the same
semantic** over **the same effective scope**, and the evidence still does not
establish the relationship:

```
AUTHORITY_REVIEW_REQUIRED
```

Report both sides symmetrically:

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
WHY AUTOMATIC RESOLUTION IS UNSAFE =
```

Then: **do not guess, do not publish the affected semantic, and continue
processing everything independent of it.** One contested family does not stall
a batch.

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
