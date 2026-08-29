"""Closed vocabularies the BillShield persistence foundation owns.

WHY THIS MODULE EXISTS
----------------------
A `CHECK (code ~ '^[A-Z][A-Z0-9_]{2,63}$')` is bounded text, not a closed
vocabulary: it accepts `WIDGET_FROBNICATED` as happily as a real state. Every
code this schema persists therefore has exactly one Python authority here (or
in the committed Slice 1 contract, for codes that already have one) and one SQL
constraint that lists the same members. `tests/unit/billshield/
test_persistence_contract_parity.py` compares the two sets and fails on any
divergence in either direction, so the constraint cannot drift from the
vocabulary and an unknown uppercase token is refused by the database.

THREE FAILURE VOCABULARIES, AND ONLY TWO OF THEM EXIST
-----------------------------------------------------
Confusing these is how a schema ends up with an open text column named as if it
were closed. They are:

1. **Extraction refusal** — the extractor READ the document and declined it as a
   whole. Authority: `extraction.codes.RefusalCode` (committed, Slice 1).
   Persisted as `billshield.extraction_run.refusal_code`.
2. **Extraction parse failure** — a provider produced output and the strict
   parse rejected it. Authority: `extraction.codes.ExtractionParseCode`
   (committed, Slice 1). Persisted as
   `billshield.extraction_run.failure_code`.
3. **Outbox / runtime failure** — a queued job could not be completed by the
   worker. Authority: DOES NOT EXIST YET; it belongs to the Slice 3 runtime.
   Persisted NOWHERE, and `billshield.job_outbox` therefore carries no
   `last_error_code` column. Adding an open-ended uppercase text column now, so
   a later slice could avoid an ALTER, would be exactly the "closed code" that
   is really free text (plan §7.1, §9.1).

Codes that already have an authority are NOT re-declared here — reuse is the
rule. `ServiceCategory` and `ArtifactFormat`, plus both extraction vocabularies
above, live in the committed Slice 1 contract, and the parity test compares each
against the SQL constraint that mirrors it.
"""
from __future__ import annotations

from enum import StrEnum

#: The one object-key layout. `billshield.bill.storage_key` is a generated
#: column computed as `user_id || '/' || STORAGE_KEY_INFIX || '/' || id`, so a
#: key can never name a row other than the one carrying it (plan §7.4).
STORAGE_KEY_INFIX = "billshield/v1"


class BillState(StrEnum):
    """The bill lifecycle of plan §7.3, as a closed set.

    Membership only. The transition graph belongs to the domain service a later
    slice adds; the database refuses states that are not states at all, which is
    a different and cheaper guarantee than refusing illegal transitions.
    """

    UPLOAD_PENDING = "upload_pending"
    UPLOADED = "uploaded"
    SCANNING = "scanning"
    REJECTED = "rejected"
    EXTRACTING = "extracting"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"
    CONFIRMED = "confirmed"
    DELETION_PENDING = "deletion_pending"
    DELETED = "deleted"


class ExtractionRunState(StrEnum):
    """Outcome of one extraction attempt.

    `REFUSED` carries a `RefusalCode`: the extractor read the document and
    declined it as a whole. `FAILED` carries an `ExtractionParseCode`: output
    arrived and the strict parse rejected it. Both authorities are committed, so
    both states persist a code — neither is the outbox/runtime vocabulary that
    does not exist yet.
    """

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    REFUSED = "refused"
    FAILED = "failed"


class OutboxTaskCode(StrEnum):
    """What a queued BillShield intent asks for.

    Exactly one member at the foundation. A queue whose task vocabulary is open
    is a queue that can be asked to do anything.
    """

    EXTRACT_BILL = "EXTRACT_BILL"


class OutboxClaimState(StrEnum):
    """Relay state of one outbox row, mirroring `ioe.freshness_outbox`.

    The API may only ever produce `PENDING` — server defaults own this column
    and the enqueue grant does not include it (plan §9.1). The transitions out
    of `PENDING` belong to the Slice 3 claim/complete/fail keyholes.
    """

    PENDING = "pending"
    CLAIMED = "claimed"
    COMPLETED = "completed"
    FAILED = "failed"


#: Attempt ceiling carried by the table's CHECK, matching the freshness relay's
#: bound (`db/sql/29_ioe_outbox_and_projection.sql`). Recorded here so the
#: parity test can compare it with the constraint rather than trusting prose.
OUTBOX_MAX_ATTEMPTS = 5

#: Evidence-locator bounds, mirrored by `billshield.evidence_locators`. The
#: upper bound is the committed contract's `max_evidence_per_field`; the parity
#: test asserts the two agree rather than restating the number as a constant a
#: reader must trust.
EVIDENCE_MIN_LOCATORS = 1

__all__ = [
    "EVIDENCE_MIN_LOCATORS",
    "OUTBOX_MAX_ATTEMPTS",
    "STORAGE_KEY_INFIX",
    "BillState",
    "ExtractionRunState",
    "OutboxClaimState",
    "OutboxTaskCode",
]
