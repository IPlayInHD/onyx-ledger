# Document-first consumer input — BLOCKED on a missing backend capability

> **UPDATE — the first blocking step is now partly closed.** A later entry
> ("start the extraction worker") added `app/services/document_processing/text.py`,
> `DocumentService.extract_from_storage`, and `workers/tasks/documents.py`.
> **Onyx now reads a stored `text/plain` or `text/csv` document itself** and
> extracts fields from its real content — so for those media types the
> `document_backed` provenance claim is true rather than a lie.
>
> **PDF and image are still not readable**, and they are what a real filer
> uploads, so the consumer journey below remains blocked. The statements marked
> ✅ CORRECTED further down were true when written and are not any more; they
> are corrected in place rather than deleted, because the reasoning that
> followed from them is still the reasoning that governed the decision.

**Status: reported, not implemented.** No application code was changed by the
entry that produced this file. The only modification is this document.

The UX audit (`zero-tax-knowledge-audit.md` §C item 3) recorded that "the
document pipeline exists in the backend and is unreachable from the UI — the
single largest automation win is built and unused". **That was half right, and
the wrong half was load-bearing.** A closer reading of the pipeline shows that
the step the whole journey depends on — Onyx reading the document — does not
exist.

## What exists

| Stage | Route | What it actually does | State |
|---|---|---|---|
| Register + upload | `POST /api/v1/documents` | Creates a `docs.document` row, returns a **presigned PUT** with a signed size ceiling. Bytes go straight to object storage, never through the API. MIME allow-list, 25 MB cap. | **Real** |
| Extract | `POST /api/v1/documents/{id}/process` | Runs `extract_fields()` over **text or fields supplied by the caller**. | **Real, but see below** |
| Confirm | `POST /api/v1/documents/{id}/confirm` | Turns the latest extraction into `IncomeSource` / `ExpenseRecord` rows with `verification_status="document_backed"` and a `DocumentLink`, then fires `on_financial_data_changed`. | **Real** |
| List | `GET /api/v1/documents` | Returns `{id, status}` **and nothing else**. | **Real, insufficient** |
| Delete | `DELETE /api/v1/documents/{id}` | Purges the binary and the extraction, tombstones the row. | **Real** |

`SLIP_MAP` in `app/services/document_processing/ocr.py` declares patterns for
**T4, T5, T2202, T2125, T776, MEDICAL, DONATION** — box-number regexes such as
`box\s*14[^0-9]{0,12}([\d,]+\.?\d*)`.

## What does not exist

**Nothing converts an uploaded document into text.** Verified four ways:

1. `extract_fields()` has exactly **one** caller — `DocumentService.process` —
   and that method's text comes from its caller, not from storage.
2. No worker reads a stored document. `workers/tasks/documents.py` **does not
   exist**, although `celery_app.py` routes `workers.tasks.documents.*` to the
   `documents` queue. The route is dead.
   *(✅ CORRECTED: the worker now exists and the route reaches it. It reads text
   media only.)*
3. **No OCR or PDF-text dependency is installed.** The only AWS/parsing library
   in `requirements.lock.txt` is `boto3`. There is no `pytesseract`,
   `pdfplumber`, `pypdf`, `pdfminer` or equivalent.
4. `DocumentService.process`'s own docstring says so: *"dev/demo: pass
   structured `fields` or OCR `text`. Production: a worker OCRs the object in
   storage and calls the same service."* **That worker was never written.**
   *(✅ CORRECTED: written. It reads the object through `ObjectStorage.get` and
   calls `DocumentService.process`, as that docstring described — but it does
   not OCR, and it refuses the media types it cannot read rather than passing
   empty text through.)*

The only integration test for the pipeline
(`tests/integration/test_documents.py`) drives `process` with structured
`fields` supplied by the test — the same path a browser would have to use.

`ObjectStorage.get(bucket, key) -> bytes` **does** exist, so storage can be read
back; the TKMS legislation pipeline already uses it. Nothing in the consumer
document path does.

## Why this blocks the entry rather than shrinking it

The requested journey is *upload → Onyx reads → user reviews → user confirms*.
With no extractor, a frontend can only reach `process` by sending `fields` —
which means **the user types the numbers and the browser posts them as an
extraction**.

That is not a smaller version of the feature. It is the opposite of it:

* The screen would say "we found this on your document" about values the user
  typed.
* Confirmation writes `verification_status="document_backed"` and a
  `DocumentLink` onto governed financial rows — recording that a document backs
  a figure Onyx never read. That is a **false evidence-provenance claim** on a
  record the engine treats as document-supported, and evidence provenance is on
  the non-negotiable list.
* The audit trail would be wrong in the direction that matters: it would
  over-state, not under-state, how well supported a number is.

The entry's own instructions cover this case — §1 *"Do not claim T4, T2202, or
any other form is supported unless repository evidence proves it"*, §7 *"do not
create a frontend-only correction model that is never persisted
authoritatively"*, and §16 *"If a significant backend feature is missing, report
it before widening the implementation."*

## Secondary gaps found on the way

1. **`GET /documents` cannot drive a document list.** It returns `{id, status}`
   — no filename, no document type, no tax year, no extraction summary. A
   consumer could not tell one uploaded document from another.
2. **No correction pathway.** Nothing accepts a corrected value for an extracted
   field before confirmation. `confirm` consumes the latest extraction as-is.
   The §7 experience ("distinguish extracted from corrected, preserve
   provenance") has no contract behind it.
3. **`process()` performs no explicit ownership check.** `confirm()` compares
   `doc.user_id` to the caller; `process()` does not. **This is not a
   vulnerability:** `docs.document` is `ENABLE + FORCE ROW LEVEL SECURITY`
   (verified live: `relrowsecurity` and `relforcerowsecurity` both true), so
   another tenant's row is invisible and the lookup raises `NotFound`. Recorded
   as a defence-in-depth observation, consistent with the repository's stated
   posture that RLS is the tenant-correctness boundary — not as a defect.
4. **Extraction text is never cross-checked against the stored object.**
   `process` sets `content_hash = sha256(text)` — a hash of the supplied text,
   not of the bytes in storage. Whoever calls `process` decides what the
   document "said". Harmless while the only caller is a trusted worker; load-
   bearing the moment a browser becomes the caller.

## What would unblock it

In dependency order. None is UI work.

1. **A document extraction worker** — ✅ **DONE for text media, still open for
   PDF and image.** `workers/tasks/documents.py` now consumes the `documents`
   queue, reads the object with `ObjectStorage.get`, and calls
   `DocumentService.process`. `text.py` names the media types it cannot read and
   records them as `failed` instead of extracting nothing from empty text.
   The dependency choice this step was really about — native-text PDF vs scanned
   image OCR, which are different problems with different honest confidences —
   is **still unmade**, and a photographed T4 still needs the second.
   Nothing enqueues the task yet either: bytes are not in storage when
   `POST /documents` returns its presigned URL, so enqueuing there would race
   the upload.
2. **An extraction-result read contract** — a way for the client to poll or
   fetch the current extraction and its fields, plus enough on `GET /documents`
   to render a list.
3. **A correction contract**, if the reviewed-and-corrected experience is
   wanted, that keeps the extracted value and the corrected value distinct.
4. **Then** the consumer UI, which at that point is genuinely a presentation
   entry.

Step 1 is a backend entry with real architectural content — where extraction
runs, what it costs, how failure is represented, and what evidence a scanned
image can honestly support. It should not be decided inside a UI entry.

## The one thing that is genuinely supported today

A `text/plain` or `text/csv` upload, where the browser reads the file it just
uploaded and posts its literal text to `process`. The regexes then extract from
the document's real content, and `document_backed` would be true.

It is honest, and it is not a consumer journey: a first-time filer has a PDF or
a photo of a T4, not a text file of one. Recorded here so the option is on the
record rather than discovered again later, and **not** built, because shipping
it as "document-first tax input" would describe the product inaccurately.


## Found while building the worker: the box-number regexes miss on real layouts

Feeding the extractor **realistic** slip text for the first time showed that
`SLIP_MAP`'s box-number patterns almost never match. Each allows at most **12**
non-digit characters between the box number and the amount, and a real T4 line
puts the box *label* in that gap:

| Line | Characters between box number and amount |
|---|---|
| `Box 14  Employment income      42,680.00` | 25 |
| `Box 16  CPP contributions       2,430.15` | 26 |
| `Box 18  EI premiums               743.60` | 28 |
| `Box 22  Income tax deducted     5,910.00` | 26 |

All four exceed the bound, so **every box-number pattern misses**. The only T4
field that extracts at all is `employmentIncome`, and not by its box 14 pattern
either — by its `employment income` *label* fallback, which is the only fallback
any T4 field has.

**Not fixed in the worker entry.** Changing these regexes changes what the
already-certified `POST /documents/{id}/process` endpoint extracts, which is a
governed extraction-behaviour change and not what "start the extraction worker"
asked for. The consequence today is contained: of the four T4 fields, only
`employmentIncome` appears in `FIELD_TARGET`, so the three that miss would reach
no financial row even if they did match. It is pinned by
`test_the_box_number_patterns_do_not_survive_a_realistic_slip_layout` so it
cannot be quietly forgotten, and that test is written to fail — deliberately —
the day the patterns are fixed.
