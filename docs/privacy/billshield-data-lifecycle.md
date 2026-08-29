# BillShield data lifecycle — deletion, retention, and what each table promises

Scope: the BillShield database foundation — the `billshield` schema created by
the foundation migration. This document records what each table holds, who can
reach it, and what happens to it when a customer asks to be forgotten. It is
the companion to two registries that the test suite enforces, and it never
contradicts them:

* `backend/app/privacy/classification.py` — the per-table `LIFECYCLE` and
  `NON_RLS` declarations, checked against the live catalogue on every run.
* `backend/tests/privacy/account_delete_registry.py` and
  `docs/privacy/11b6-account-delete-cascade-universe.md` — the certified
  account-deletion universe, which no table may enter without a classification
  argued from evidence.

**No duration in this document is approved.** Retention is expressed as policy
names, which is what `RetentionClass` deliberately provides; the actual periods
are recommendations in the integration plan awaiting legal and privacy
sign-off. Nothing here should be read as a commitment to a number.

## The seven tables

| Table | Kind | Privacy classes | Source | Retention | Account deletion | RLS |
|---|---|---|---|---|---|---|
| `billshield.provider` | Global catalogue | — (`NON_RLS`) | reference data | operator-managed | not reached | none, justified |
| `billshield.provider_category` | Global catalogue | — (`NON_RLS`) | reference data | operator-managed | not reached | none, justified |
| `billshield.bill` | Tenant root | `DOCUMENT_BINARY`, `PSEUDONYMOUS_IDENTIFIER` | `SOURCE` | `RETAINED_PENDING_REVIEW` | `CUSTOM_WORKFLOW` | ENABLE + FORCE |
| `billshield.extraction_run` | Tenant-derived | `DOCUMENT_EXTRACTED_DATA`, `FINANCIAL_SOURCE_DATA` | `DERIVED` | `RETAINED_PENDING_REVIEW` | `CASCADE_DELETE` | ENABLE + FORCE |
| `billshield.charge_candidate` | Tenant-derived | `DOCUMENT_EXTRACTED_DATA`, `FINANCIAL_SOURCE_DATA` | `DERIVED` | `RETAINED_PENDING_REVIEW` | `CASCADE_DELETE` | ENABLE + FORCE |
| `billshield.promotion_candidate` | Tenant-derived | `DOCUMENT_EXTRACTED_DATA` | `DERIVED` | `RETAINED_PENDING_REVIEW` | `CASCADE_DELETE` | ENABLE + FORCE |
| `billshield.job_outbox` | Tenant-derived | `PSEUDONYMOUS_IDENTIFIER`, `OPERATIONAL_TELEMETRY` | `OPERATIONAL` | `SHORT_OPERATIONAL` | `CASCADE_DELETE` | ENABLE + FORCE |

## Ownership and the RLS path

Every tenant-derived table is confined by `ENABLE` **and** `FORCE` row-level
security, with one `FOR ALL` policy whose `USING` and `WITH CHECK` are
identical and resolve through `ref.current_app_user()`. `ENABLE` is what
confines the ordinary roles; `FORCE` additionally subjects the table owner. A
`USING`-only policy would read correctly and leave ownership forgery open,
which is why both halves are always written.

| Table | Ownership | How the policy resolves it |
|---|---|---|
| `bill` | direct `user_id` | `user_id = ref.current_app_user()` |
| `extraction_run` | through the bill | `EXISTS (… billshield.bill b WHERE b.id = bill_id AND b.user_id = current)` |
| `charge_candidate` | through run → bill | one explicit join up the chain |
| `promotion_candidate` | through run → bill | one explicit join up the chain |
| `job_outbox` | direct `user_id`, plus a composite edge | `user_id = ref.current_app_user()`, and `(bill_id, user_id)` must name a real bill of that same tenant |

Children carry **no denormalized `user_id`**. A denormalized ownership column
is a way to *change* ownership, so the chain is walked explicitly instead. With
no `app.user_id` set, `ref.current_app_user()` is NULL, `NULL = anything` is
never true, and all five tables expose zero rows.

Two structural constraints do work no policy can do:

* `job_outbox (bill_id, user_id)` references `bill (id, user_id)`. Without it, a
  row naming tenant A beside tenant B's bill satisfies a policy that only checks
  `user_id`.
* `extraction_run (bill_id, input_sha256)` references `bill (id, file_sha256)`.
  A run therefore describes the finalized bytes of the very bill it names, and
  cannot be repointed at another artifact.

## The global tables, and why they carry no RLS

`provider` and `provider_category` hold commercial reference data — a
provider's identity and which service categories it offers. They contain no
tenant data, have no foreign-key path from `identity.user_account`, and are
therefore absent from the account-deletion universe by construction. RLS on
them would protect nothing and would break the shared catalogue every tenant
reads, so their waiver is a reviewed `NON_RLS` entry with `user_derived=False`.
Least privilege for them is the grant: `SELECT` to the API and nothing else.
No administrative writer exists yet; rows arrive by migration or operator
action under the migrator role until the catalogue slice defines curation.

**A provider has many categories.** `provider` deliberately has no `category`
column: a Canadian provider commonly spans mobile, internet, television, home
phone and bundles, and a single scalar would have to misrepresent all but one.

**No issuer inference.** `extraction_run.issuer_name_value` is untrusted text
read off a document. Nothing joins it to a `provider` row, and no column exists
that could hold such a resolution. A deterministic issuer→provider resolver,
with explicit user confirmation for ambiguity, belongs to the catalogue slice.

## Logical deletion versus physical erasure

These are two different facts and the schema keeps them apart:

* **`deleted_at` — logical deletion.** Set when the customer's deletion request
  is accepted. The bill moves to `deletion_pending` and the API stops serving
  the artifact immediately. It is not a claim that any byte is gone.
* **`erased_at` — physical erasure.** Stamped only after the privacy worker has
  proven every object version and delete marker for that key is gone. The bill
  is then `deleted`.

A database constraint keeps the pair honest: `deletion_pending` requires
`deleted_at` present and `erased_at` absent; `deleted` requires both; any other
state requires neither; and erasure can never precede deletion. A single
timestamp meaning both would let "deleted" mean *we stopped showing it* or *the
bytes are gone* depending on who was reading.

Four distinct mechanisms, in the order they apply:

1. **Logical deletion** (this slice's schema): the tombstone above, written by
   the API through a column-scoped update that cannot touch `erased_at`.
2. **Physical erasure of the source object** (**Slice 3**): the privacy worker
   deletes every object version and stamps `erased_at`. Nothing in the
   foundation erases an object, and no runtime here holds object-version
   delete authority.
3. **Account-root cascade** (this slice): every tenant-derived table cascades
   from `identity.user_account`. `bill` and `job_outbox` at depth 1 by their own
   `user_id`; `extraction_run` at depth 2 through the bill; the two candidate
   tables at depth 3 through the run.
4. **Retained tombstone or provenance:** none in the foundation. Every
   BillShield row is removed by the account cascade; nothing is de-identified
   and retained. Where later slices introduce retained evidence, it will be
   classified before it ships.

Because the source object outlives the row that names it until the worker runs,
`billshield.bill` is `CUSTOM_WORKFLOW` rather than `CASCADE_DELETE`: the
extended `DOCUMENTS` privacy phase must erase the object **before** the cascade
removes the row carrying its key. Both domains prove convergence independently.

## Immutable candidates, and where a correction goes

Extraction candidates are immutable extracted facts, enforced by trigger rather
than by convention. A user's correction is **not** an edit to a candidate row:
it becomes a structurally distinct confirmed-observation record in a later
slice. Two reasons, both load-bearing:

* An extraction must stay reproducible. Its response hash is computed over the
  candidates a strict parse accepted; editing one silently invalidates the
  identity of a run somebody already read.
* "Extracted" and "confirmed" are different claims about the world. Merging
  them into one mutable row destroys the distinction that makes an audit
  possible, and the product promises exactly that distinction.

`billshield.bill` identity is likewise immutable: `id`, `user_id` and the
generated `storage_key` never change, and the finalized artifact facts —
digest, byte size, media format, page count — move from unset to set once and
never again.

## What the outbox may contain

`job_outbox` holds identifiers, one closed task code (`EXTRACT_BILL`), an
opaque dedupe key, and relay bookkeeping. There is **no column** that can hold
a charge, an amount, a label, a filename, or a provider's exception text — the
only durable way to keep bill content out of a queue is to give it nowhere to
go. Worker identity is a bounded opaque token, not prose.

It carries **no failure-code column**, and that is a narrower statement than it
may look. Three failure vocabularies are in play and only two exist today:

* *extraction refusal* — the extractor read the bill and declined it whole;
  authority `RefusalCode`, stored on `extraction_run.refusal_code`;
* *extraction parse failure* — output arrived and the strict parse rejected it;
  authority `ExtractionParseCode`, stored on `extraction_run.failure_code`;
* *outbox/runtime failure* — a queued job the worker could not complete. Its
  authority belongs to the Slice 3 runtime and does not exist yet, so no column
  holds it. An open-ended uppercase text column now would be a "closed code"
  that is really free text.

The API's authority on this table is a **column-scoped insert** on the four
intent columns only. Server defaults own `claim_state`, `attempts`, and every
claim and terminal field, so a request path cannot create work that is already
claimed, completed, or failed, and holds no `UPDATE` or `DELETE`. Claim,
complete and fail are Slice 3 keyholes.

## `NoCandidate` is not "absent from the document"

Where an extraction produced no candidate for a field, all three of its
columns — value, confidence, evidence — are NULL together. That records
something about the **extractor**, not about the bill: the model offered no
evidence-backed value. It is never a statement that the document lacks the
field, and no reader, report, or later slice may present it as one. The
extractor holds no authority over what a document contains.

Evidence locators are governed by the `billshield.evidence_locators` domain: a
bounded array of 1–16 objects with exactly the keys `page`, `x0`, `y0`, `x1`,
`y1`; a positive integer page; and canonical unit-interval decimal **strings**
for coordinates. The closed key set is what makes "evidence carries no text
snippet" structural rather than aspirational. Canonical ordering and
duplicate-freeness within a list are enforced by the committed parser before
anything is persisted, and proven end-to-end by the contract-parity test, which
fails if a stored extraction stops reproducing its own hash.

## Retention, and what is still owed

| Data | Policy name in the registry | Recommended period (NOT approved) |
|---|---|---|
| Incomplete upload | `RETAINED_PENDING_REVIEW` | short, hours-scale erase after abandonment |
| Failed or rejected source bill | `RETAINED_PENDING_REVIEW` | short erase unless the customer retries |
| Confirmed source bill | `RETAINED_PENDING_REVIEW` | erase after confirmation; keep structured observations |
| Unconfirmed candidates | `RETAINED_PENDING_REVIEW` | with the source bill |
| Raw provider response | not stored at all | — |
| Queued job intent | `SHORT_OPERATIONAL` | drained in normal operation |
| Catalogue rows | operator-managed reference data | retained for comparison history |

The right-hand column exists to show that the engineering can express these
policies, not to assert them. Legal and privacy owners set the actual periods
before production; until they do, `RetentionClass` names the policy and the
retention worker that would enforce a duration does not exist.

For the state graph, both terminal failure states reach erasure: a `rejected`
or `failed` bill can move to `deletion_pending`, so a retention rule for
unusable bills is executable rather than merely stated.
