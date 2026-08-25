"""Document extraction — read what the customer uploaded.

THE QUEUE THIS DRAINS WAS ALREADY ROUTED AND HAD NOTHING BEHIND IT.
`celery_app.py` has sent `workers.tasks.documents.*` to the `documents` queue
since the queue table was written, and this module did not exist, so the route
went nowhere. `DocumentService.process`'s own docstring described the worker
that would call it — "Production: a worker OCRs the object in storage and calls
the same service" — and that worker was never written. Without it the only way
to reach extraction was for the CALLER to supply the text, which for a browser
would mean the customer typing numbers that the pipeline then recorded as
`document_backed`: a provenance claim about a document Onyx never opened.

WHAT THIS DOES AND DOES NOT READ. Text documents today. A PDF's text layer and
a photograph of a slip are different problems with different dependencies and
different honest confidences, and `document_processing/text.py` names the gap
rather than guessing at it. A media type this cannot read is recorded as
`failed`, never as a successful extraction with no fields — empty text runs
through the regexes happily and yields nothing, which reads as "we read your T4
and it was blank".

THE TENANT CONTEXT COMES FROM THE ENQUEUER. The task takes a user id as well as
a document id, because a worker cannot look the owner up first: `docs.document`
is FORCE row level security, so reading the row requires the context this
argument establishes. That is not a hole. A caller who enqueues a mismatched
pair gets nothing — RLS scopes the lookup to the user in the GUC, so a document
that user does not own is simply not there, and the task ends in `NotFound`.

NOTHING ENQUEUES THIS YET. The trigger belongs with the upload contract: bytes
are not in storage when `POST /documents` returns a presigned URL, so enqueuing
there would race the upload. Wiring it is the next entry; this one is the
capability.
"""
from __future__ import annotations

import uuid

from celery import Task

from app.core.exceptions import NotFound
from app.core.logging import get_logger
from app.database.session import unit_of_work
from app.services.document_processing.service import DocumentService
from app.services.document_processing.text import DocumentUnreadable
from app.services.privacy.preflight import refuse_if_deleting
from workers.celery_app import celery_app
from workers.runtime import run_task

log = get_logger("onyx.worker")

#: Reading an object and running regexes over it is cheap, and a document that
#: cannot be read will not become readable on the fourth attempt. Retries are
#: for the storage read, which can fail transiently.
MAX_RETRIES = 3


@celery_app.task(
    name="workers.tasks.documents.extract_document", bind=True, max_retries=MAX_RETRIES
)
def extract_document(self: Task, document_id: str, user_id: str) -> dict:
    """Extract one uploaded document, as its owner.

    Returns a small, closed summary — never the extracted values. A Celery
    return value is written to the result backend, and the numbers on a tax
    slip are the most sensitive thing in the product.
    """

    async def _run() -> dict:
        async with unit_of_work(user_id=uuid.UUID(user_id), actor_type="system") as session:
            # THE DELETION CUTOFF, before anything is read or written.
            #
            # This task may have been queued long before the account asked to be
            # deleted, and the queue is not a place a refusal can be applied: a
            # reserved task is already in a worker's hands, `acks_late`
            # redelivers it after a restart, and an offline worker never sees a
            # revoke. Extraction writes an extraction row, its fields and a
            # document status — user data, for an account being erased.
            if await refuse_if_deleting(session, uuid.UUID(user_id),
                                        task="documents.extract_document"):
                return {"document_id": document_id, "status": "refused"}
            extraction = await DocumentService(session).extract_from_storage(
                uuid.UUID(user_id), uuid.UUID(document_id)
            )
            return {
                "document_id": document_id,
                "status": extraction.status,
                "engine": extraction.engine,
            }

    try:
        result = run_task(_run)
    except DocumentUnreadable as unreadable:
        # NOT a retry and NOT an error: the refusal is the outcome, it is
        # already recorded against the document, and a second attempt would
        # read the same bytes and decline again.
        log.info("document_extraction_refused",
                 operation="extract_document", outcome="refused",
                 reason=unreadable.reason, document_id=document_id)
        return {"document_id": document_id, "status": "failed", "reason": unreadable.reason}
    except NotFound:
        # The document is gone, or belongs to somebody else. Either way there
        # is nothing to extract and nothing to retry.
        log.info("document_extraction_absent",
                 operation="extract_document", outcome="absent",
                 document_id=document_id)
        return {"document_id": document_id, "status": "absent"}
    except ValueError as exc:
        # A malformed identifier will never become well-formed. No retry.
        log.error("document_extraction_invalid",
                  operation="extract_document", outcome="failed",
                  reason="MALFORMED_IDENTIFIER", error_type=type(exc).__name__)
        raise self.retry(exc=exc, max_retries=0) from exc
    except Exception as exc:  # noqa: BLE001
        log.error("document_extraction_failed",
                  operation="extract_document", outcome="failed",
                  reason="EXTRACTION_FAILED", error_type=type(exc).__name__,
                  document_id=document_id)
        raise self.retry(exc=exc, countdown=2 ** self.request.retries) from exc

    log.info("document_extracted",
             operation="extract_document", outcome="succeeded",
             document_id=document_id, status=result["status"])
    return result
