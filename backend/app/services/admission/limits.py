"""Named payload and complexity bounds.

Every maximum in Entry 10 lives here rather than as a literal at a call site.
A magic number in a handler is invisible to review as part of a set, cannot be
audited against the others, and drifts from the operation it was meant to
describe. These names appear in the error messages too, so a rejected caller and
a reader of this file are looking at the same limit.

COMPLEXITY IS NOT THE SAME AS SIZE. A request can be tiny in bytes and enormous
in work: a hundred scenario levers is a few hundred bytes and a combinatorial
assembly problem. Both kinds of bound are collected here, and the ones that
already existed elsewhere are re-exported rather than restated, so there is
exactly one number per limit.
"""
from __future__ import annotations

from app.services.ioe.domain.scenario import (
    MAX_ASSUMPTIONS_PER_SCENARIO,
    MAX_LEVERS_PER_SCENARIO,
)

# --- documents -------------------------------------------------------------
#: A CRA slip is kilobytes; a scanned multi-page return is a few megabytes.
#: 25 MB accepts a generous real document and refuses an upload used as storage.
MAX_DOCUMENT_BYTES = 25 * 1024 * 1024

#: One upload request describes one document. Batching is not supported, so a
#: count above one is a client bug or an attempt to amortise the rate limit.
MAX_DOCUMENTS_PER_REQUEST = 1

#: Filenames are echoed into object keys and logs. Long enough for any real
#: name, short enough that it cannot be used as a payload channel.
MAX_FILENAME_LENGTH = 255

#: OCR text handed to the extraction path in the dev/demo flow.
MAX_EXTRACTION_TEXT_BYTES = 1 * 1024 * 1024

#: Structured extraction fields in one request.
MAX_EXTRACTION_FIELDS = 200

#: The content classes the pipeline can actually parse. An allow-list, not a
#: deny-list: a deny-list admits every format nobody thought of.
ALLOWED_DOCUMENT_MIME_TYPES = frozenset({
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/tiff",
    "image/heic",
    "text/plain",
    "text/csv",
})

# --- legislation import ----------------------------------------------------
#: Raw import payload. Legislation documents are large; this is well above any
#: real one and far below a memory problem.
MAX_IMPORT_BYTES = 50 * 1024 * 1024

#: Rows one import may stage. Bounds the staged-row write and the review queue.
MAX_IMPORT_ROWS = 10_000

# --- calculation complexity (already enforced in the domain; named here so the
#     complete set of bounds is readable in one place) ----------------------
MAX_SCENARIO_LEVERS = MAX_LEVERS_PER_SCENARIO
MAX_SCENARIO_ASSUMPTIONS = MAX_ASSUMPTIONS_PER_SCENARIO


__all__ = [
    "ALLOWED_DOCUMENT_MIME_TYPES",
    "MAX_DOCUMENTS_PER_REQUEST",
    "MAX_DOCUMENT_BYTES",
    "MAX_EXTRACTION_FIELDS",
    "MAX_EXTRACTION_TEXT_BYTES",
    "MAX_FILENAME_LENGTH",
    "MAX_IMPORT_BYTES",
    "MAX_IMPORT_ROWS",
    "MAX_SCENARIO_ASSUMPTIONS",
    "MAX_SCENARIO_LEVERS",
]
