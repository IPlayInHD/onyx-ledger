# TAX KNOWLEDGE SOURCE REGISTRY (Entry: Source Registry + Provenance Foundation)

The governed answer to: *which authoritative source, and which part of it,
supports this Onyx rule, formula or reference-data value?*

```
tax_kb.tax_source          the continuing publication  (Income Tax Act, T4044)
    └── tax_kb.tax_source_version   one retrieved edition + content fingerprint
            └── tax_kb.source_citation      edition + STRUCTURED locator
                    └── tax_kb.knowledge_citation   what the citation supports
                            ├── rule version   (deadlines + evidence inherit)
                            ├── formula
                            └── reference data (brackets, limits, parameters)
```

Internal tooling only. No customer route reaches it, and the rules evaluator
never reads it.

## 1. Why new tables rather than the existing stubs

The repository already had two source-shaped tables. Both were measured before
anything was designed:

| | Existing | Consequence |
|---|---|---|
| `tax_kb.gov_source` | `(id, name, url, created_at)`, **0 rows** | no version, issuer, jurisdiction, fingerprint, effective period or supersession |
| `tax_kb.legislation_reference` | `(id, citation, title, url)`, **1 row** | `citation` is one free-form text column — no structured locator |
| `tax_rule_version` | single nullable FK to each, plus a redundant `source_url` | **one** citation per rule version; `_load_citations` literally returns a one-tuple |

So: one source can support many rules, but a rule version cannot cite many
locations; formulas and reference data have **no source column at all**; and
because a `gov_source` row is shared and mutable, editing it silently rewrites
what every rule referencing it claims to rest on.

The stubs are left untouched and unread. Nothing migrates them, because
fabricating provenance for historical test data is exactly what §15 forbids.

## 2. Source is not rule

A source document is evidence for an interpretation. It is never executable tax
logic. There is no path from a PDF to an eligibility decision, no LLM in this
entry, and no retrieval at runtime — an official locator is **recorded**, never
fetched. `RulesEvaluatorService` and `TaxEngineService` keep their authority
untouched; a test greps the evaluator to prove it never reads a registry table.

## 3. Identity, and immutability without a mutable status

**Source identity** is `(jurisdiction, issuer, official_identifier)` — so the
same identifier from a different authority or jurisdiction is a different
source, not a duplicate.

**Version identity** is `(source, content_fingerprint)`. Identity is *content*,
not retrieval: a different URL query string, local path or fetch timestamp
describes how a document was obtained, not which document it is. Re-importing
identical bytes is idempotent.

**A different fingerprint is a different edition.** It never overwrites.

**Supersession points backward from the successor** (`supersedes_version_id`),
so no historical row is ever rewritten and "superseded" is *derived* from the
successor's existence rather than written into the predecessor — it cannot go
stale. `UNIQUE (supersedes_version_id)` keeps the chain linear. Supersession is
never inferred from dates or titles: a caller names the predecessor or it does
not happen.

## 4. Fingerprints say which claim they are

`RAW_BYTES_SHA256` is SHA-256 over the exact bytes. `DECLARED_BY_OPERATOR` is a
digest supplied without them. The method is stored because a digest of a
document and a digest of somebody's extracted text are different claims, and a
registry storing only the hex string would let the weaker read as the stronger.

Raw bytes are hashed with the standard primitive rather than the canonicalizer —
deliberately. Canonicalization makes *structured values* comparable; a document's
identity is its literal bytes, and canonicalizing a PDF before hashing would
produce a digest of our reading of it.

Source files are **untrusted input**: hashed and discarded, never parsed,
executed or interpreted. Nothing about a filename is identity.

**Raw content is not stored.** Metadata + official locator + fingerprint only.
Object storage is not configured for this data class and §12 says not to broaden
the entry into a document-management system; the registry proves source identity
without it.

## 5. Locators are structured, and that makes citations reusable

A locator is a closed key set — section, subsection, paragraph, clause, page,
table, schedule, form_line, heading, anchor — validated on the way in. Its
identity is `domain_hash(source_locator, …)` over the canonical (sorted) form,
so field order cannot fork a citation: the same location in the same edition is
**one row**, cited by many knowledge objects, instead of the source metadata
being copied into each.

The same section under two editions stays distinguishable, because the *edition*
is part of citation identity even when the locator digest matches.

## 6. What each knowledge kind gets, and why

| Kind | Provenance | Reasoning |
|---|---|---|
| Rule version | explicit link | many citations per version, which the old single FK could not express |
| Deadline | **inherited** | hangs off a rule version with no independent existence; a separate link would be a second answer |
| Evidence requirement | **inherited** | same |
| Formula | **explicit** | a formula is shared across rules, so "the rule that uses it" stops identifying anything the moment two do |
| Reference data | **explicit** | brackets, limits and indexed parameters are read by the engine directly and never pass through a rule version — nothing could be inherited, which is exactly how they become the unexplained constants §17 forbids |

`knowledge_citation` uses an **exclusive arc** — one nullable FK per kind, exactly
one required by CHECK — so every link keeps real referential integrity. A
`(kind, object_id)` pair would have been one column shorter and would have let a
citation point at a formula that no longer exists.

## 7. No fallback to the current web

Resolution returns the **pinned** edition. A newer edition existing is reported
as `superseded` and changes nothing: §22's distinction between *superseded* and
*integrity failure* is the whole point, because a historical rule was authored
against particular words.

If the pinned edition is missing, resolution raises `ProvenanceUnavailable`. It
never substitutes today's version, which would answer a different question from
the one asked.

## 8. Security — the part that matters

`tax_kb` carries DEFAULT PRIVILEGES granting `onyx_app_rw` **arwd**, so creating
a table there would silently hand the customer runtime role the ability to
insert, rewrite and delete authoritative tax law. The migration revokes it.

| Role | Registry access |
|---|---|
| `onyx_app_rw` (customers) | **SELECT only** |
| `onyx_app_ro` | SELECT |
| `onyx_kb_admin` (authoring) | SELECT + **INSERT only** — no UPDATE, no DELETE |

Corrections happen by registering a new version and superseding, never by
editing history. Nothing is exposed through `SECURITY DEFINER`, and no
application route can write these tables at all — asserted by scanning the
OpenAPI document for any source/citation/provenance path.

## 9. Privacy — a different data class entirely

Tax law is global reference knowledge. The tables carry **no `user_id`**, have no
RLS (matching every existing `tax_kb` table), and are not reachable from
`identity.user_account`. The customer privacy universe is therefore **unchanged
at 73** — the full privacy suite passes with no registry entries added, which is
the evidence rather than the claim.

Nothing here stores operator identity, review notes, uploaded local filenames or
annotations. User-uploaded personal tax documents remain an entirely separate
data class and are never mixed with the source registry.

Deletion: source versions are not connected to account deletion and no retention
period is invented. Retirement is **logical supersession**, never destruction of
a referenced version.

## 10. Versioning

`SOURCE_MANIFEST_SCHEMA_VERSION` is the import contract's own version, stored on
every version row, independent of the rule, scenario, assurance, lifecycle and
retention versions. A manifest written for a different contract is refused
rather than reinterpreted.

## 11. Measured

- **Provenance resolution does not N+1**: 1 citation and 25 citations both cost
  **6 statements**. The first version of that test watched the wrong engine and
  reported a passing `0 == 0`; the counter now asserts it saw real traffic.
- **Schema drift 0** on a freshly migrated database, with 16 governed
  divergences registered under existing classes.
- **Determinism**: locator digests invariant under `PYTHONHASHSEED` 0/1/42.
- **No tax-result change**: the rules evaluator references no registry table,
  and the closed-entry suites are unchanged.
