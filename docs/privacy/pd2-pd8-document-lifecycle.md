# PD-2 and PD-8 — the document and object-storage lifecycle (Entry 11B4)

Two defects on one surface, which is why they are one entry: what a document's
object key *contains*, and whether a document's binary can ever be *removed*.
Fixing either alone leaves the other holding the same data.

| | Before | After |
|---|---|---|
| **PD-2** object key | `{user_id}/{uuid4}/{filename}` | `{user_id}/v2/{document_id}` |
| **PD-8** deletion | no delete on the port, no route, no caller | `ObjectStorage.delete`, `DocumentService.delete_document`, `DELETE /api/v1/documents/{id}` |

## PD-2 — what a filename is

A filename is user free text, and in this product specifically it is free text
about a person's tax affairs. `Zorana Marchetti 2025 T4 medical private.pdf`
names the person, the employer relationship, the year, and what the document is
about, and every one of those is the kind of fact the rest of this system keeps
behind row-level security.

An object key is not behind row-level security. Keys surface in bucket
listings, CDN and access logs, provider consoles, support tooling and billing
exports — places with entirely different access control from the row, and none
of them reached by deleting anything in PostgreSQL. The key was also returned
to the client by `GET /api/v1/documents`, so the filename travelled back out on
every listing.

The key is now server-generated and every component is an identifier this
system minted:

```
{user_id}/v2/{document_id}
```

No filename, no extension, no title, no tax year. The MIME type lives in
`docs.document.mime_type`, which is where it belongs — a content type in a path
is decoration, and once an extension is allowed the argument for the stem
follows immediately. The `{user_id}/` prefix stays on Entry 11A's
recommendation: it is an internal UUID, and it makes an account-level purge of
object storage one prefix operation rather than a row-by-row walk.

`filename` is still accepted by the route and still validated there — a client
sending an oversized one is still refused — and is then deliberately not stored
anywhere. `docs.document` has no filename column, and the route no longer
returns `object_key`.

The `v2` marker is not decoration: it lets a legacy key be recognised without
parsing it, which is what the inventory in `scripts/document_storage_audit.py`
depends on.

## PD-8 — a binary that nothing could remove

`ObjectStorage` had `presign_put`, `presign_get`, `put` and `get`. There was no
`delete`, no service method that wanted one, and no route. A user's document
binary, once uploaded, had no path out of the system at all — which also meant
Entry 11B2's account deletion could never have been completed, because the
storage phase had nothing to call.

The port now declares `delete(bucket, key) -> DeleteOutcome`, with a closed
outcome set:

| Outcome | Meaning |
|---|---|
| `DELETED` | the object was there and is gone |
| `ALREADY_ABSENT` | it was not there; the lifecycle may still advance |
| `RETRYABLE_FAILURE` | try again; nothing was changed |
| `PERMANENT_FAILURE` | this will not succeed by repetition |

Closed on purpose. A provider message would be the natural thing to return and
is exactly what must not be: Entry 11A proved exception text in this stack
carries financial values, and this outcome is logged, metered and returned.

`ALREADY_ABSENT` counts as success. A deletion phase that failed because the
object was already gone would never converge, and the only thing worse than a
binary outliving its document is a lifecycle that can never finish removing it.

## What deletion actually does

`DocumentService.delete_document(user_id, document_id)`:

1. take a transaction-scoped advisory lock on the document (see *Races*);
2. load the row and refuse anything the caller does not own;
3. if already tombstoned, return `already_deleted=True` and stop;
4. delete the object through the port; on `RETRYABLE_FAILURE` or
   `PERMANENT_FAILURE`, raise `DocumentDeletionFailed` (503) having changed
   **nothing** in the database;
5. purge the extraction and its fields, set-based;
6. set `deleted_at`.

### Why the row is tombstoned rather than deleted

Hard-deleting it would cascade `docs.document_link` away, and Entry 11A is
explicit that the provenance edge must survive: a confirmed tax figure must
never look unsourced. It would also discard the content hash. `deleted_at` is
the mechanism the schema already provided and every read path already honoured
— Entry 11A described it as an affordance "read by every query path, written by
none". This entry is what starts writing it.

The object key is **not** cleared. It is opaque now, so it identifies the
object that was removed without saying anything about the person, and keeping
it is what lets an orphan check distinguish *deleted on purpose* from
*vanished*.

### What each class of data does

| Data | On document deletion |
|---|---|
| the binary | removed from object storage |
| `docs.document_extraction` | purged |
| `docs.extraction_field` | purged |
| `docs.document` | tombstoned; `content_hash` and `object_key` retained |
| `docs.document_link` | retained, now pointing at a tombstone |
| confirmed income/expense rows | **retained** — Entry 11A policy |
| a sealed analysis snapshot | untouched |

The confirmed figures survive because they are the user's tax position, not a
copy of the document. Deleting a receipt does not un-spend the money. The
provenance edge survives with them so the figure never becomes unsourced —
`test_the_provenance_edge_knows_its_source_is_gone` proves the edge is still
there and that it resolves to a deleted document.

## Ordering, and why there is no outbox

Object storage and PostgreSQL are not one transaction. One of them commits
first and a crash between them is possible. The storage delete goes first and
is idempotent, which makes retry converge without a state machine:

| Crash point | State | Retry |
|---|---|---|
| storage fails | nothing changed | deletes again, completes |
| storage succeeded, database fails | binary gone, row still live | `ALREADY_ABSENT`, then finishes the database work |

The residual risk is a document that reads as live with no binary behind it if
nobody ever retries. That is **reported rather than prevented** — see
*Remaining limitations* — and it is the honest cost of not building a durable
job for work that is one object deletion and two bounded statements.

## Races

Processing and deletion are separate transactions with nothing ordering them.
The interleaving that matters:

```
deletion:   purge extraction ── commit "deleted" ──▶
processing:      read document ─────────────── write fields ──▶
```

The document then reports deleted while holding the extracted contents of the
binary that was removed. §25 names that as the outcome that must never happen,
and the test written for it **reproduced it on the first run**:

```
AssertionError: the document reports deleted and still has an extraction:
the processor wrote fields after the deletion completed
```

Fixed with a transaction-scoped advisory lock keyed on the document id, taken
first by both `process` and `delete_document`, plus a `deleted_at` guard in
`process`. Whichever transaction arrives first finishes; the second sees the
committed result:

- deletion first → the processor reads a tombstone and refuses with `NotFound`;
- processing first → the fields exist and are then purged.

Both are outcomes §25 permits. Keyed on the document, so two different
documents never wait for each other — the same shape Entry 11B2 used to order
writes against the account deletion cutoff.

Confirmation races the same way and is covered by
`test_confirmation_and_deletion_reach_a_coherent_end_state`: either the
confirmed rows exist with a retained provenance edge, or the confirmation is
refused. Never a confirmed figure with no edge at all.

## Cross-tenant deletion

Impossible by construction, not by check alone. The route takes a document id
and **nothing else** — no bucket, no key. The key is resolved server-side from
the owned row. A caller able to name its own key could ask the platform to
delete an arbitrary object, including another tenant's, and the ownership check
would never see it; `test_the_delete_route_never_accepts_a_bucket_or_key` is a
signature assertion that keeps it that way.

Beyond that: RLS hides the row, the service compares `user_id` and raises
`NotFound` rather than a distinguishable forbidden (which would confirm the id
exists), and `test_one_tenant_cannot_delete_anothers_document` asserts through a
storage spy that **no object deletion was attempted at all** — not merely that
the request failed.

## Minimization of the deletion itself

A tool for handling a privacy defect must not become one.

| Surface | Contains |
|---|---|
| API response | `{"status": "deleted", "already_deleted": bool}` |
| exception detail | a closed `DeleteOutcome` value |
| audit | unchanged — no filename, no key, no provider text |
| metrics | `operation`, `outcome`, `reason_code` only |
| `scripts/document_storage_audit.py` | counts only, never a key or an id |

`already_deleted` is the one thing beyond the state, and it is the caller's own
retry telling it whether this call did the work.

## Object versioning — RESOLVED in B2A

On a versioned S3 bucket, `DeleteObject` writes a delete marker and **the
previous versions remain**. That is not erasure. The choice was: turn
versioning off, or enumerate and delete every version. B2A did the second, so
versioning stays on and erasure still means the bytes are gone.

`ObjectStorage.hard_erase` — the port's only erasure operation — lists every
version and delete marker for exactly one key, filters the listing to an exact
key match so a sibling under the same prefix is never touched, removes them by
version id in batches, and then re-lists to confirm none remain. A delete
marker is never treated as erasure, and an unconfirmed erasure is not recorded
as one.

**What this section used to say, corrected.** Until B2 it read
"`get_object_storage()` returns `LocalObjectStorage()` unconditionally" and
"the S3 adapter exists only as a commented sketch". Both were true when written
and neither is true now: B2 wrote the adapter, made the provider a
configuration choice, and made production refuse the in-memory store outright.

The historical claim it rested on still stands and is worth keeping, because it
bounds what any deployed environment can be holding: up to B2, no commit on any
branch made `get_object_storage` return an S3 adapter, so no code in this
repository had ever written a byte to a persistent object store. That is a
statement about the repository, not a guarantee about any environment.

What remains under **PD-10** is object-store encryption, TLS and backup
configuration — `DEPLOYMENT_CONFIGURATION_REQUIRED`, and not closed by this.

## Historical legacy keys — `OPERATIONAL_REVIEW_REQUIRED`

Any `docs.document` row created before this entry carries a filename-bearing
key, and that filename is personal data at rest in PostgreSQL independently of
what any bucket holds.

`scripts/document_storage_audit.py` counts them — opaque against legacy — and
reports nothing else. It is the instrument for deciding, because the answer
depends on a database this repository cannot see.

Disposition, if the count is non-zero:

- **Rewriting the column alone is wrong.** The key is the pointer to the
  object. Rewriting it without moving the object orphans the binary, which
  still carries the filename in *its* key, and breaks retrieval besides.
- **The correct migration is copy → verify → delete original → update row**,
  per document, against a live provider. That is an operator procedure, not a
  schema migration, and it cannot be run or proven from the repository.
- Given the versioning section above, the expected count in any environment
  running this code is **zero** — but that is an inference from the repository,
  not a measurement of an environment, and it is not offered as proof.

The audit script also reports the crash-window states from *Ordering*:
tombstoned documents still holding an extraction, and live documents with no
key. It exits non-zero on the former.

What it **cannot** report is bucket objects with no database owner. That needs
a bucket listing, and the port has no list operation — deliberately, since
nothing in the product needs one. Stated rather than silently omitted: an
orphan sweep against a real provider needs either a list capability or a
provider-side inventory report.

## The `server/` application

The Node/Express application under `server/` is classified
`SEPARATE_APPLICATION` (Entry 11B2 §14) and is covered by **PD-14**, which is
out of scope here. For PD-2 and PD-8 specifically:

- **PD-8 does not apply.** There is no object storage. `server.js` uses
  `multer.memoryStorage()`, decodes the buffer to text and discards it; nothing
  is written to a bucket or to disk as a binary. There is no object to orphan,
  and `store.deleteDocument` already removes the record.
- **PD-2 does not apply as stated.** There are no object keys, so no key can
  embed a filename.

Neither of those is a clean bill of health, and two findings are recorded here
for PD-14 rather than fixed:

1. `server.js` persists `name: req.file.originalname` **and the full decoded
   document text** into the user record, and `GET /api/documents` and the
   export endpoint return the whole record. That is the same personal data
   PD-2 is about, stored in Netlify Blobs, merely not in a key.
2. `store.deleteDocument` removes the document but leaves `u.audit`, which was
   computed from the documents. Derived data outliving its source is the exact
   shape this entry fixed on the Python side.

Deleting a document in the Python application does nothing to any of it.

## Cost

Measured by `scripts/probe_document_lifecycle_cost.py`.

| `create_upload` | statements | p50 | p95 |
|---|---|---|---|
| INSERT + UPDATE (first cut) | 4 | 4.64 ms | 6.83 ms |
| INSERT only (current) | 3 | 3.91 ms | 4.71 ms |

Deriving the key from the document's own id means the id must exist before the
key does. Letting the column default supply it meant INSERT, then UPDATE the
key — a second round trip on every upload forever, to fix a defect about what
the key *contains*. The service assigns `uuid7()` instead, which the helper
documents as byte-for-byte identical to `ref.uuid_generate_v7()`, so rows
written either way are indistinguishable and sort together. The column keeps
its server default for every other writer.

| `delete_document` | statements | p50 | p95 |
|---|---|---|---|
| 0 extracted fields | 5 | 4.83 ms | 8.48 ms |
| 1 extracted field | 7 | 6.52 ms | 9.59 ms |
| 40 extracted fields | 7 | 7.10 ms | 9.08 ms |

Constant in the field count — the extraction purge is two set-based statements,
not N. That is what makes the synchronous route defensible rather than merely
convenient. If object deletion ever becomes slow enough to matter (a versioned
bucket needing per-version deletes), the route returns 202 and grows a job
*then*, on evidence.

The §25 advisory lock is one statement, 0.380 ms p50 uncontended.

## Schema

None. No column added, no table created, no migration written. `deleted_at`
already existed; this entry is the first thing to write it. The migration
round-trip and fresh-database drift check both pass unchanged, with zero
unexpected drift.

## Proof

30 tests across two files.

`tests/security/test_pd2_pd8_object_storage.py` — the key carries no part of
the filename even for a filename built out of a name, an employer and a
condition; the filename is stored nowhere; two identical filenames get
different keys; the API never hands the key back; the port declares a delete;
deletion removes the bytes, is idempotent, treats a missing object as success,
and releases the upload authorization; the outcome set is closed. Plus
regression guards: the key builder takes no user-supplied argument, no
production code builds a document key from a filename, the service deletes
through the port rather than around it, and the route signature accepts neither
a bucket nor a key.

`tests/security/test_document_deletion_lifecycle.py` — binary purged;
extraction purged; confirmed facts survive with provenance retained and marked
source-deleted; the row tombstoned keeping its content hash; gone from the
listing; deleting twice converges; a retry completes after the object is
already gone; a storage failure returns 503 and leaves the document deletable
with nothing tombstoned; cross-tenant deletion attempts no object deletion at
all; an unknown id is 404; document work stops after the account deletion
cutoff (Entry 11B2 preserved); a sealed analysis is unchanged; and the two race
tests above.

## Remaining limitations

1. **No orphan sweep.** The crash window in *Ordering* is reported, not
   prevented. Closing it needs either a durable job or a bucket listing.
2. **Object versioning is resolved** as of B2A. `hard_erase` enumerates every
   version and delete marker for exactly one key, removes them by version id,
   and re-lists to confirm none remain — so a versioned bucket is a supported
   production configuration rather than a reason erasure cannot complete. What
   PD-10 still covers is object-store encryption, TLS and backup configuration,
   which stay `DEPLOYMENT_CONFIGURATION_REQUIRED`.
3. **Historical legacy keys** in a deployed database need an operator
   migration — `OPERATIONAL_REVIEW_REQUIRED`.
4. **No backup or PITR erasure claim.** A tombstoned document's binary is gone
   from the live store; what any backup holds is Entry 11A's tombstone work and
   is not implemented.
5. **`server/` is not reconciled** — PD-14, open.
6. **Account-level object purge is not implemented.** The `{user_id}/` prefix
   makes it a single prefix operation, and nothing yet performs it.
